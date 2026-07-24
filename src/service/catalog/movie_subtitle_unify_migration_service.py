"""字幕统一迁移：把两处分裂的 ``.srt`` 收敛到 ``movies/<shard>/<番号>/subtitles/``。

用户通过 CLI ``python -m src.start.commands migrate-movie-subtitles`` 手动触发；不进 lifespan。
建议在 ``migrate-movie-asset-shard`` 之后运行，让目标目录已经是分片布局（非强制：目标目录会按需
``mkdir``，先跑本迁移也只是把 subtitles/ 建在分片路径上，后续资产分片的整目录搬运会自然合并）。

存量字幕分布在两处：

- 本地媒体：作为 sidecar 跟视频放在媒体库 ``<库根>/jav/<番号>/<版本时间戳>/<番号>.srt``
- 115 云盘媒体（``Media.path`` 为 NULL，没有本地目录可放）：``<旧字幕根>/<番号>/<编码名>.srt``

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

硬链接语义保证即使中途掉电，数据始终被至少一条路径 hold 住。``file_path`` 已落在统一目录下的行
走 fast-path skip，重跑幂等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from loguru import logger

from src.common.media_paths import movie_subtitle_dir
from src.common.subtitle_paths import subtitle_root_path
from src.model import Movie, Subtitle
from src.service.transfers.file_transfer import transfer_file


# 迁移进度回调：(current, total)
ProgressCallback = Callable[[int, int], None]

SUBTITLE_EXTENSION = ".srt"


@dataclass
class MigrationStats:
    # 以 Subtitle 表为准逐行迁移
    subtitles_scanned: int = 0
    subtitles_migrated: int = 0          # 完整 3 步
    subtitles_recovered: int = 0         # 中断态补 STEP 2
    subtitles_skipped_ok: int = 0        # 已在统一目录，fast-path
    subtitles_data_lost: int = 0         # 新旧路径都不存在
    subtitles_conflict_skipped: int = 0  # (movie, file_path) 已被同影片另一行占用
    subtitles_failed: int = 0
    failed_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "subtitles_scanned": self.subtitles_scanned,
            "subtitles_migrated": self.subtitles_migrated,
            "subtitles_recovered": self.subtitles_recovered,
            "subtitles_skipped_ok": self.subtitles_skipped_ok,
            "subtitles_data_lost": self.subtitles_data_lost,
            "subtitles_conflict_skipped": self.subtitles_conflict_skipped,
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

    # ---- 以 Subtitle 表为准逐行迁移 ----

    @classmethod
    def _migrate_registered_subtitles(
        cls,
        stats: MigrationStats,
        *,
        dry_run: bool,
        progress: ProgressCallback | None,
    ) -> None:
        rows = list(Subtitle.select(Subtitle, Movie).join(Movie).order_by(Subtitle.id.asc()))
        total = len(rows)
        for index, subtitle in enumerate(rows, start=1):
            stats.subtitles_scanned += 1
            try:
                cls._migrate_one_subtitle(subtitle, stats, dry_run=dry_run)
            except Exception as exc:
                logger.warning(
                    "subtitle unify failed subtitle_id={} path={} detail={}",
                    subtitle.id, subtitle.file_path, exc,
                )
                stats.subtitles_failed += 1
                stats.failed_paths.append(subtitle.file_path)
            if progress is not None:
                progress(index, total)

    @classmethod
    def _migrate_one_subtitle(cls, subtitle: Subtitle, stats: MigrationStats, *, dry_run: bool) -> None:
        movie_number = subtitle.movie.movie_number
        target_directory = movie_subtitle_dir(movie_number)
        old_path = Path(subtitle.file_path).expanduser()

        if cls._is_within(old_path, target_directory):
            stats.subtitles_skipped_ok += 1
            return

        new_path = target_directory / cls._build_target_name(old_path, subtitle.id)
        old_exists = old_path.is_file()
        new_exists = new_path.is_file()

        if not old_exists and not new_exists:
            logger.error(
                "subtitle unify data lost subtitle_id={} old={} new={}",
                subtitle.id, old_path, new_path,
            )
            stats.subtitles_data_lost += 1
            return

        # (movie, file_path) 有 unique 索引，目标被同影片另一行占用时跳过交人工排查。
        conflict_exists = (
            Subtitle.select(Subtitle.id)
            .where(
                Subtitle.movie == subtitle.movie_id,
                Subtitle.file_path == str(new_path),
                Subtitle.id != subtitle.id,
            )
            .exists()
        )
        if conflict_exists:
            logger.warning(
                "subtitle unify skip conflict subtitle_id={} old={} new={}",
                subtitle.id, old_path, new_path,
            )
            stats.subtitles_conflict_skipped += 1
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

    @classmethod
    def _build_target_name(cls, old_path: Path, subtitle_id: int) -> str:
        """统一目录是平的，文件名必须在同一部影片内唯一。

        - 旧字幕根内的文件（115 导入）：文件名已带 fid 去重，原样保留
        - 媒体库 sidecar：同番号多次导入都叫 ``<番号>.srt``，改用所在版本目录名（导入时间戳）
        """
        if cls._is_within(old_path, subtitle_root_path()):
            return old_path.name
        version_name = old_path.parent.name
        if not version_name:
            return f"{subtitle_id}{SUBTITLE_EXTENSION}"
        return f"{version_name}{SUBTITLE_EXTENSION}"

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
