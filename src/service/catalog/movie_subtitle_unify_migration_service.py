"""字幕统一迁移：把两处分裂的 ``.srt`` 收敛到 ``movies/<shard>/<番号>/subtitles/<番号>-<N>.srt``。

用户通过 CLI ``python -m src.start.commands migrate-movie-subtitles`` 手动触发；不进 lifespan。
建议在 ``migrate-movie-asset-shard`` 之后运行，让目标目录已经是分片布局（非强制：目标目录会按需
``mkdir``，先跑本迁移也只是把 subtitles/ 建在分片路径上，后续资产分片的整目录搬运会自然合并）。

存量字幕分布在三处：

- 本地媒体：作为 sidecar 跟视频放在媒体库 ``<库根>/jav/<番号>/<版本时间戳>/<原名>.srt``
  （whisperjav 会在同版本目录里同时生成 ``.chinese.srt``/``.srt`` 等多份）
- 115 云盘媒体（``Media.path`` 为 NULL，没有本地目录可放）：``<旧字幕根>/<番号>/<编码名>.srt``
- 已跑过老版本迁移的遗产：``<subtitles>/<版本时间戳>.srt``（旧命名，本轮统一为 ``<番号>-<N>.srt``）

命名规则统一为 ``<番号>-<N>.srt``（N 从当前 subtitles/ 目录已有序号 max + 1 起），由
``src.common.media_paths.allocate_next_movie_subtitle_path`` 分配。同一部影片下多份 sidecar 与
115 老字幕共享同一序号空间，天然不撞车。

设计核心 —— 单文件 3 步，``subtitle.file_path`` 的单条 UPDATE 是原子提交点：

    STEP 1  os.link 到新路径；跨文件系统（媒体库 vs /data/cache）降级 shutil.copy2；已存在则跳
    STEP 2  UPDATE subtitle SET file_path = <新路径>        ← 原子提交点
    STEP 3  unlink 旧文件（best-effort，失败仅告警，下次重跑再删）

按 ``(old_exists, new_exists)`` 两态收敛到已迁移终态：

    | old_exists | new_exists | 动作 |
    |-----------|-----------|------|
    | ✗ | ✗ | 数据丢失: log error + skip，记录留给 sync_movie_subtitles 清理 |
    | ✓ | ✗ | 完整 STEP 1 → 2 → 3 |
    | ✗ | ✓ | 中断态: 只补 STEP 2（文件已就位，DB 未提交） |
    | ✓ | ✓ | 中断态: STEP 2 + best-effort 删 old |

硬链接语义保证即使中途掉电，数据始终被至少一条路径 hold 住。``file_path`` 已落在统一目录下
且文件名符合 ``<番号>-<N>.srt`` 格式的行走 fast-path skip，重跑幂等。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from src.common.media_paths import (
    allocate_next_movie_subtitle_path,
    is_movie_subtitle_target_name,
    movie_subtitle_dir,
)
from src.model import Movie, Subtitle
from src.service.transfers.file_transfer import transfer_file

# 迁移进度回调：(current, total)
ProgressCallback = Callable[[int, int], None]


@dataclass
class MigrationStats:
    # 以 Subtitle 表为准逐行迁移
    subtitles_scanned: int = 0
    subtitles_migrated: int = 0          # 完整 3 步
    subtitles_recovered: int = 0         # 中断态补 STEP 2
    subtitles_skipped_ok: int = 0        # 已在统一目录 & 命名规范，fast-path
    subtitles_data_lost: int = 0         # 新旧路径都不存在
    subtitles_failed: int = 0
    failed_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "subtitles_scanned": self.subtitles_scanned,
            "subtitles_migrated": self.subtitles_migrated,
            "subtitles_recovered": self.subtitles_recovered,
            "subtitles_skipped_ok": self.subtitles_skipped_ok,
            "subtitles_data_lost": self.subtitles_data_lost,
            "subtitles_failed": self.subtitles_failed,
            "failed_paths": list(self.failed_paths),
        }


class MovieSubtitleUnifyMigrationService:
    """把媒体库 sidecar 与旧字幕根里的 ``.srt`` 统一搬到番号资产目录下的 ``subtitles/``。"""

    @classmethod
    def run(
        cls,
        *,
        dry_run: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> MigrationStats:
        stats = MigrationStats()
        cls._migrate_registered_subtitles(stats, dry_run=dry_run, progress=progress_callback)
        return stats

    # ---- 以 Subtitle 表为准，按 movie 分组处理（同影片共享 seq 分配器） ----

    @classmethod
    def _migrate_registered_subtitles(
        cls,
        stats: MigrationStats,
        *,
        dry_run: bool,
        progress: ProgressCallback | None,
    ) -> None:
        # 一次性拉齐全部 Subtitle + Movie，按 movie 聚合；同一部影片的字幕共用一个 seq 分配器，
        # 避免"同一批里两条 subtitle 抢同一个 <num>-N.srt"。
        rows = list(Subtitle.select(Subtitle, Movie).join(Movie).order_by(Subtitle.id.asc()))
        total = len(rows)
        by_movie: dict[int, list[Subtitle]] = {}
        for subtitle in rows:
            by_movie.setdefault(subtitle.movie_id, []).append(subtitle)

        processed = 0
        for subtitles in by_movie.values():
            movie_number = subtitles[0].movie.movie_number
            # reserved_names 累计本 movie 已分配（DB 已 UPDATE 或本轮 dry-run 计划中的）新文件名，
            # 保证同一 movie 的多条字幕拿到不同 seq。
            reserved_names: set[str] = set()
            for subtitle in subtitles:
                processed += 1
                stats.subtitles_scanned += 1
                try:
                    cls._migrate_one_subtitle(
                        subtitle,
                        movie_number=movie_number,
                        reserved_names=reserved_names,
                        stats=stats,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    logger.warning(
                        "subtitle unify failed subtitle_id={} path={} detail={}",
                        subtitle.id, subtitle.file_path, exc,
                    )
                    stats.subtitles_failed += 1
                    stats.failed_paths.append(subtitle.file_path)
                if progress is not None:
                    progress(processed, total)

    @classmethod
    def _migrate_one_subtitle(
        cls,
        subtitle: Subtitle,
        *,
        movie_number: str,
        reserved_names: set[str],
        stats: MigrationStats,
        dry_run: bool,
    ) -> None:
        target_directory = movie_subtitle_dir(movie_number)
        old_path = Path(subtitle.file_path).expanduser()

        # 已经在统一目录且文件名符合 <番号>-<N>.srt 格式 → 终态，登记 seq 后跳过。
        # （只判目录内 + 格式匹配，不检查磁盘文件是否存在——data_lost 由 sync_movie_subtitles 收尾。）
        if cls._is_within(old_path, target_directory) and is_movie_subtitle_target_name(
            movie_number, old_path.name,
        ):
            reserved_names.add(old_path.name)
            stats.subtitles_skipped_ok += 1
            return

        # 分配一个本 movie 未占用的 <番号>-<N>.srt 目标路径，登入 reserved 供下一条字幕避让。
        new_path = allocate_next_movie_subtitle_path(
            movie_number, reserved_names=reserved_names,
        )
        reserved_names.add(new_path.name)

        old_exists = old_path.is_file()
        new_exists = new_path.is_file()

        if not old_exists and not new_exists:
            logger.error(
                "subtitle unify data lost subtitle_id={} old={} new={}",
                subtitle.id, old_path, new_path,
            )
            stats.subtitles_data_lost += 1
            return

        if dry_run:
            if new_exists and not old_exists:
                stats.subtitles_recovered += 1
            else:
                stats.subtitles_migrated += 1
            return

        if old_exists and not new_exists:
            target_directory.mkdir(parents=True, exist_ok=True)
            # transfer_file 内部先 os.link，跨文件系统时自动降级 shutil.copy2。
            transfer_file(old_path, new_path)                 # STEP 1
            cls._commit_file_path(subtitle.id, new_path)      # STEP 2 原子提交
            cls._delete_source(old_path)                      # STEP 3
            stats.subtitles_migrated += 1
            return

        if not old_exists and new_exists:
            # 中断态：文件已就位，DB 未提交。补 STEP 2。
            cls._commit_file_path(subtitle.id, new_path)
            stats.subtitles_recovered += 1
            return

        # 两边都在：中断态（STEP 1 完成、STEP 2/3 未做）。
        cls._commit_file_path(subtitle.id, new_path)
        cls._delete_source(old_path)
        stats.subtitles_migrated += 1

    @staticmethod
    def _is_within(file_path: Path, root_path: Path) -> bool:
        # 两侧都 resolve，避免根目录含符号链接时判定失败。
        try:
            file_path.resolve().relative_to(root_path.resolve())
        except (ValueError, OSError):
            return False
        return True

    @staticmethod
    def _commit_file_path(subtitle_id: int, target: Path) -> None:
        """STEP 2：单条 UPDATE 是原子提交点。"""
        Subtitle.update(file_path=str(target)).where(Subtitle.id == subtitle_id).execute()

    @staticmethod
    def _delete_source(old_path: Path) -> None:
        """STEP 3：删旧文件；失败仅告警不抛，下次重跑还能再删。"""
        try:
            old_path.unlink()
        except OSError as exc:
            logger.warning("subtitle unify unlink old failed path={} detail={}", old_path, exc)
