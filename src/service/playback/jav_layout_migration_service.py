"""JAV 存储布局迁移服务：把老平铺 ``<root>/番号/`` 迁到 ``<root>/jav/番号/``。

用户通过 CLI ``python -m src.start.commands migrate-jav-layout`` 手动触发；不进 lifespan。

设计核心 —— 单文件 4 步流程，DB update 是唯一原子提交点：

    STEP 1  os.link(old_path, new_path)     # 硬链接；跨 fs 时 fallback shutil.copy2
    STEP 2  Media.path = new_path; save()   # 原子提交点：从这一刻起系统认为已在新位置
    STEP 3  os.unlink(old_path)             # best-effort；失败不 raise，下次可再删
    STEP 4  同目录 sidecar 顺次做 STEP 1 + STEP 3；清理空老目录

启动扫描每个 Media 记录，按 ``(db_path 前缀, old_exists, new_exists)`` 三元状态收敛到 A
（未迁移）或 F（已迁移）两种终态：

    | db 前缀   | old_exists | new_exists | 动作 |
    |-----------|-----------|-----------|------|
    | old-style | ✗ | ✗ | 数据丢失: log critical + 标 Media.valid=False |
    | old-style | ✓ | ✗ | A 未迁移: 完整 STEP 1→2→3→4 |
    | old-style | ✗ | ✓ | 中断态: 只做 STEP 2 |
    | old-style | ✓ | ✓ | 中断态: 从 STEP 2 继续（跳过 STEP 1） |
    | new-style | ✓ | ✓ | 中断态: 补 STEP 3 |
    | new-style | ✗ | ✓ | F 已迁移: 无动作 |
    | new-style | ✓ | ✗ | 严重不一致: 回滚 DB 到 old-style |
    | new-style | ✗ | ✗ | 数据丢失 |
    | 其他前缀  | — | — | Media.path 不属于本库两种布局: 不动 |

硬链接语义保证：即使 crash，数据被至少一个路径 hold 住，永不丢失。
STEP 1 幂等（``new_path`` 已存在直接跳）；STEP 2 是原子提交点；STEP 3/4 best-effort。
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from loguru import logger

from src.model import Media, MediaLibrary
from src.model.enums import MediaLibraryBackend
from src.service.transfers.file_transfer import JAV_LIBRARY_SUBDIR


ProgressCallback = Callable[[int, int, int], None]  # (current, total, library_id)


@dataclass
class MigrationStats:
    """迁移统计 —— stats 字段按含义分组，便于 CLI/日志展示。"""

    libraries_scanned: int = 0
    # 状态推进
    media_migrated: int = 0           # A → F 完整迁移
    media_recovered_to_f: int = 0     # 中断态恢复到 F
    media_rolled_back: int = 0        # DB 是新但新缺失、老在: 回滚 DB
    # 无动作
    media_skipped_ok_f: int = 0                # 已经 F, fast-path
    media_skipped_unknown_layout: int = 0      # Media.path 前缀两种布局都不是
    # 异常
    media_data_lost: int = 0                   # 两个路径都不存在
    media_failed: int = 0                      # 处理时抛异常
    # 附加
    sidecars_migrated: int = 0
    failed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "libraries_scanned": self.libraries_scanned,
            "media_migrated": self.media_migrated,
            "media_recovered_to_f": self.media_recovered_to_f,
            "media_rolled_back": self.media_rolled_back,
            "media_skipped_ok_f": self.media_skipped_ok_f,
            "media_skipped_unknown_layout": self.media_skipped_unknown_layout,
            "media_data_lost": self.media_data_lost,
            "media_failed": self.media_failed,
            "sidecars_migrated": self.sidecars_migrated,
            "failed_files": list(self.failed_files),
        }


class JavLayoutMigrationService:
    """按 Media 记录粒度把 JAV 从 ``<root>/<num>/`` 迁到 ``<root>/jav/<num>/``。

    单文件 4 步流程；DB update 为原子提交点；中断可恢复。失败 log 不 raise。
    """

    @classmethod
    def run(
        cls,
        *,
        library_id: int | None = None,
        dry_run: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> MigrationStats:
        """迁移入口。``library_id`` 为 None 时处理全部本地库；``dry_run`` 只统计不动。

        重跑幂等：所有已达 F 的 media 走 fast-path skip；处于中间态的会被恢复。
        """
        stats = MigrationStats()
        query = MediaLibrary.select().where(
            MediaLibrary.backend == MediaLibraryBackend.LOCAL.value
        )
        if library_id is not None:
            query = query.where(MediaLibrary.id == library_id)
        for library in query:
            stats.libraries_scanned += 1
            cls._process_library(library, stats, dry_run=dry_run, progress=progress_callback)
        return stats

    @classmethod
    def _process_library(
        cls,
        library: MediaLibrary,
        stats: MigrationStats,
        *,
        dry_run: bool,
        progress: ProgressCallback | None,
    ) -> None:
        root = Path(library.backend_config["root_path"]).expanduser()
        if not dry_run:
            # 顶层 jav 目录预先建好；已存在幂等
            (root / JAV_LIBRARY_SUBDIR).mkdir(exist_ok=True)
        # 拉这个库所有 JAV 记录（movie_number 非空）
        media_rows = list(
            Media.select().where(
                Media.library == library,
                Media.movie_number.is_null(False),
            )
        )
        total = len(media_rows)
        for idx, media in enumerate(media_rows, start=1):
            try:
                cls._process_media(library, media, root, stats, dry_run=dry_run)
            except Exception as exc:
                logger.warning(
                    "jav layout migrate failed media_id={} path={} detail={}",
                    media.id,
                    media.path,
                    exc,
                )
                stats.media_failed += 1
                stats.failed_files.append(media.path)
            if progress is not None:
                progress(idx, total, library.id)

    @classmethod
    def _process_media(
        cls,
        library: MediaLibrary,
        media: Media,
        root: Path,
        stats: MigrationStats,
        *,
        dry_run: bool,
    ) -> None:
        """判定 Media 当前处于状态矩阵哪一格，分派到对应处理函数。"""
        num = media.movie_number
        # 使用严格前缀 <root>/<num>/ 和 <root>/jav/<num>/，避免误伤同名子串
        old_prefix = str(root / num) + os.sep
        new_prefix = str(root / JAV_LIBRARY_SUBDIR / num) + os.sep
        db_path = media.path

        if db_path.startswith(new_prefix):
            expected_old = Path(old_prefix + db_path[len(new_prefix):])
            expected_new = Path(db_path)
            cls._handle_db_new(media, expected_old, expected_new, stats, dry_run=dry_run)
            return

        if db_path.startswith(old_prefix):
            expected_old = Path(db_path)
            expected_new = Path(new_prefix + db_path[len(old_prefix):])
            cls._handle_db_old(media, expected_old, expected_new, stats, dry_run=dry_run)
            return

        # 前缀不匹配任一布局：例如用户手工挪过，不敢动
        logger.info(
            "jav layout skip unknown layout media_id={} path={} num={}",
            media.id, db_path, num,
        )
        stats.media_skipped_unknown_layout += 1

    # ---- 决策表两大分支 ----

    @classmethod
    def _handle_db_old(
        cls,
        media: Media,
        old: Path,
        new: Path,
        stats: MigrationStats,
        *,
        dry_run: bool,
    ) -> None:
        old_exists, new_exists = old.exists(), new.exists()
        if not old_exists and not new_exists:
            logger.error(
                "jav layout data lost media_id={} old={} new={}",
                media.id, old, new,
            )
            stats.media_data_lost += 1
            if not dry_run:
                # 顺手标 media invalid，MediaFileScanService 巡检口径一致
                Media.update(valid=False).where(Media.id == media.id).execute()
            return
        if old_exists and not new_exists:
            # A → F：完整 4 步
            if not dry_run:
                cls._create_new_link(old, new)                              # STEP 1
                cls._commit_db_path(media, new)                              # STEP 2 原子提交
                cls._delete_source(old)                                      # STEP 3
                cls._migrate_sidecars_and_prune(old.parent, new.parent, stats)  # STEP 4
            stats.media_migrated += 1
            return
        if not old_exists and new_exists:
            # 中断态：硬链接已建 + 老已被删 + DB 未 update → 只做 STEP 2
            if not dry_run:
                cls._commit_db_path(media, new)
            stats.media_recovered_to_f += 1
            return
        # 两者都在：中断态（硬链接完成，DB 未 update）→ 跳过 STEP 1，从 STEP 2 开始
        if not dry_run:
            cls._commit_db_path(media, new)
            cls._delete_source(old)
            cls._migrate_sidecars_and_prune(old.parent, new.parent, stats)
        stats.media_recovered_to_f += 1

    @classmethod
    def _handle_db_new(
        cls,
        media: Media,
        old: Path,
        new: Path,
        stats: MigrationStats,
        *,
        dry_run: bool,
    ) -> None:
        old_exists, new_exists = old.exists(), new.exists()
        if new_exists and not old_exists:
            # 已达终态 F
            stats.media_skipped_ok_f += 1
            return
        if new_exists and old_exists:
            # DB 已 update，老残留 → 补 STEP 3 + sidecar
            if not dry_run:
                cls._delete_source(old)
                cls._migrate_sidecars_and_prune(old.parent, new.parent, stats)
            stats.media_recovered_to_f += 1
            return
        if not new_exists and old_exists:
            # 严重不一致：DB 说在新但新不存在，老还在 → 回滚 DB 到 old-style
            logger.error(
                "jav layout db points to new but only old exists, rolling back "
                "media_id={} db={} old={}",
                media.id, media.path, old,
            )
            if not dry_run:
                cls._commit_db_path(media, old)
            stats.media_rolled_back += 1
            return
        # 两者都无
        logger.error(
            "jav layout data lost media_id={} old={} new={}",
            media.id, old, new,
        )
        stats.media_data_lost += 1
        if not dry_run:
            Media.update(valid=False).where(Media.id == media.id).execute()

    # ---- 底层原语 ----

    @staticmethod
    def _create_new_link(old: Path, new: Path) -> None:
        """STEP 1：优先硬链接；跨 fs 时降级复制。已存在直接跳。"""
        new.parent.mkdir(parents=True, exist_ok=True)
        if new.exists():
            return  # 幂等
        try:
            os.link(old, new)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                # 跨 fs 罕见情况：降级为复制（保留 metadata）
                shutil.copy2(old, new)
            else:
                raise

    @staticmethod
    def _commit_db_path(media: Media, target: Path) -> None:
        """STEP 2：单条 UPDATE 是原子提交点（Postgres autocommit）。"""
        Media.update(path=str(target)).where(Media.id == media.id).execute()

    @staticmethod
    def _delete_source(old: Path) -> None:
        """STEP 3：删源文件；失败仅告警不 raise，下次启动可再删。"""
        try:
            if old.exists():
                old.unlink()
        except OSError as exc:
            logger.warning("jav layout unlink old failed path={} detail={}", old, exc)

    @classmethod
    def _migrate_sidecars_and_prune(
        cls,
        old_dir: Path,
        new_dir: Path,
        stats: MigrationStats,
    ) -> None:
        """STEP 4：把 ``old_dir`` 里除主视频外的 sidecar (subtitle 等) 也硬链接到
        ``new_dir`` 并删源；然后清理最深处的空目录直到遇到非空目录为止。
        """
        if old_dir.exists() and old_dir.is_dir():
            for entry in list(old_dir.iterdir()):
                if not entry.is_file():
                    continue
                target = new_dir / entry.name
                try:
                    cls._create_new_link(entry, target)
                    stats.sidecars_migrated += 1
                    entry.unlink()
                except Exception as exc:
                    logger.warning(
                        "jav sidecar migrate failed src={} dst={} detail={}",
                        entry, target, exc,
                    )
        # 清理空目录（幂等）：从最深往上删两层，触到非空即停
        cls._prune_empty_dirs(old_dir)

    @staticmethod
    def _prune_empty_dirs(deepest: Path) -> None:
        """从 ``deepest`` 往上删空目录，遇到非空停止。只删两层（版本目录 + 番号目录）。"""
        candidate = deepest
        for _ in range(2):
            try:
                if candidate.exists() and candidate.is_dir() and not any(candidate.iterdir()):
                    candidate.rmdir()
                    candidate = candidate.parent
                else:
                    return
            except OSError:
                return
