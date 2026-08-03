"""影片剧照路径迁移：把 ``movies/<num>/plots/<i>.<ext>`` 迁到 ``movies/<num>/plot-<i>.<ext>``。

用户通过 CLI ``python -m src.start.commands migrate-plot-layout`` 手动触发；不进 lifespan。

设计核心 —— 3 步流程，``image.origin`` 单条 UPDATE 是原子提交点：

    STEP 1  os.rename(old_path, new_path)                # 同目录改名，同 fs 天然原子
    STEP 2  UPDATE image SET origin/small/medium/large   # 原子提交点：从此以新路径为准
    STEP 3  best-effort 清理空 plots/ 中间目录

扫 ``image`` 表 ``origin`` 匹配 ``movies/*/plots/*`` 的行，按 ``(old_exists, new_exists)``
两态收敛到 F（已迁移）：

    | old_exists | new_exists | 动作 |
    |-----------|-----------|------|
    | ✗ | ✗ | 数据丢失: log error + skip（不删记录，交人工排查） |
    | ✓ | ✗ | 完整 STEP 1 → 2 → 3 |
    | ✗ | ✓ | 中断态: 只做 STEP 2（rename 已完成，DB 未提交） |
    | ✓ | ✓ | 中断态: STEP 2 + best-effort 删 old |

``image.origin`` 有 unique 约束；若目标 origin 已被另一行 image 占用（罕见的历史脏数据 /
并发导入），本行 skip 并 log warning 交人工排查，避免 UPDATE 抛 IntegrityError 中断迁移。
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from src.config.config import settings
from src.model import Image

# 迁移进度回调：(current, total)
ProgressCallback = Callable[[int, int], None]

# 匹配 movies/<num>/plots/<idx>.<ext>；<num> 与 <idx>.<ext> 严格不含 "/"，避免误伤跨层路径。
_OLD_PLOT_PATTERN = re.compile(r"^movies/([^/]+)/plots/([^/]+)$")

# 进度节流：每 1000 行 或 每 10 秒 打一次 log；保证 30w 规模下也能在启动/CLI 日志里持续看到心跳。
_PROGRESS_LOG_EVERY_ROWS = 1000
_PROGRESS_LOG_EVERY_SECONDS = 10.0


@dataclass
class MigrationStats:
    images_scanned: int = 0
    # 状态推进
    images_migrated: int = 0            # 完整迁移 或 中断态补 STEP 2 + 清 old
    images_recovered: int = 0           # 中断态只补 STEP 2（rename 已完成，DB 未提交）
    # 无动作
    images_skipped_ok: int = 0          # 保留字段，当前 LIKE 查询本身不会命中新格式
    # 异常
    images_data_lost: int = 0           # old/new 两处都不存在
    images_conflict_skipped: int = 0    # 目标 origin 被另一行占用，交人工排查
    images_failed: int = 0              # 处理时抛异常
    # 附加
    empty_dirs_pruned: int = 0
    failed_origins: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "images_scanned": self.images_scanned,
            "images_migrated": self.images_migrated,
            "images_recovered": self.images_recovered,
            "images_skipped_ok": self.images_skipped_ok,
            "images_data_lost": self.images_data_lost,
            "images_conflict_skipped": self.images_conflict_skipped,
            "images_failed": self.images_failed,
            "empty_dirs_pruned": self.empty_dirs_pruned,
            "failed_origins": list(self.failed_origins),
        }


class PlotLayoutMigrationService:
    """按 image 记录粒度把剧照从 ``plots/N.ext`` 平铺成 ``plot-N.ext``。

    3 步流程；``image.origin`` 单条 UPDATE 为原子提交点；重跑幂等，中断可恢复。
    """

    @classmethod
    def run(
        cls,
        *,
        dry_run: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> MigrationStats:
        """迁移入口。``dry_run`` 只统计不动。

        重跑幂等：已达 F（origin 已是新格式）的行本次 LIKE 查询天然不会命中；处于中间态
        （文件已 rename、DB 未 UPDATE）的会被恢复到 F。
        """
        stats = MigrationStats()
        image_root = cls._image_root_path()
        # LIKE 查询无法命中 origin unique 索引，image 表 30w 级扫描仍在可接受范围。
        candidates = list(
            Image.select(Image.id, Image.origin).where(
                Image.origin ** "movies/%/plots/%"
            )
        )
        total = len(candidates)
        started_at = time.monotonic()
        last_log_at = started_at
        logger.info(
            "Plot layout migration started total={} dry_run={} image_root={}",
            total, dry_run, str(image_root),
        )
        for idx, image in enumerate(candidates, start=1):
            stats.images_scanned += 1
            try:
                cls._process_image(image, image_root, stats, dry_run=dry_run)
            except Exception as exc:
                logger.warning(
                    "plot layout migrate failed image_id={} origin={} detail={}",
                    image.id,
                    image.origin,
                    exc,
                )
                stats.images_failed += 1
                stats.failed_origins.append(image.origin)
            if progress_callback is not None:
                progress_callback(idx, total)
            # 进度节流打点：满足"每 N 行 或 每 M 秒"任一条件，末尾一定打一次。
            now = time.monotonic()
            if (
                idx == total
                or idx % _PROGRESS_LOG_EVERY_ROWS == 0
                or now - last_log_at >= _PROGRESS_LOG_EVERY_SECONDS
            ):
                elapsed = now - started_at
                rate = idx / elapsed if elapsed > 0 else 0.0
                eta_seconds = (total - idx) / rate if rate > 0 else 0.0
                logger.info(
                    "Plot layout migration progress current={}/{} migrated={} recovered={} "
                    "conflict_skipped={} data_lost={} failed={} elapsed_ms={} "
                    "rate_per_sec={:.1f} eta_ms={}",
                    idx, total, stats.images_migrated, stats.images_recovered,
                    stats.images_conflict_skipped, stats.images_data_lost, stats.images_failed,
                    int(elapsed * 1000), rate, int(eta_seconds * 1000),
                )
                last_log_at = now
        logger.info(
            "Plot layout migration finished total={} migrated={} recovered={} "
            "conflict_skipped={} data_lost={} failed={} empty_dirs_pruned={} "
            "elapsed_ms={}",
            total, stats.images_migrated, stats.images_recovered,
            stats.images_conflict_skipped, stats.images_data_lost, stats.images_failed,
            stats.empty_dirs_pruned, int((time.monotonic() - started_at) * 1000),
        )
        return stats

    @classmethod
    def _process_image(
        cls,
        image: Image,
        image_root: Path,
        stats: MigrationStats,
        *,
        dry_run: bool,
    ) -> None:
        old_origin = image.origin
        match = _OLD_PLOT_PATTERN.match(old_origin)
        if match is None:
            # LIKE 命中但正则不匹配（例如 <num> 里含额外斜杠），保守跳过。
            return
        movie_number, plot_filename = match.group(1), match.group(2)
        new_origin = f"movies/{movie_number}/plot-{plot_filename}"
        old_path = image_root / old_origin
        new_path = image_root / new_origin

        # 冲突检查：目标 origin 已被别的 image 行占用；直接 UPDATE 会撞 unique(origin)。
        conflict_exists = (
            Image.select(Image.id)
            .where(Image.origin == new_origin, Image.id != image.id)
            .exists()
        )
        if conflict_exists:
            logger.warning(
                "plot layout migrate skip conflict image_id={} old_origin={} new_origin={}",
                image.id, old_origin, new_origin,
            )
            stats.images_conflict_skipped += 1
            return

        old_exists = old_path.is_file()
        new_exists = new_path.is_file()

        if not old_exists and not new_exists:
            logger.error(
                "plot layout migrate data lost image_id={} old={} new={}",
                image.id, old_path, new_path,
            )
            stats.images_data_lost += 1
            return

        if dry_run:
            # dry-run 只报告"如果真跑会归到哪个桶"，不做任何副作用。
            if new_exists and not old_exists:
                stats.images_recovered += 1
            else:
                stats.images_migrated += 1
            return

        if old_exists and not new_exists:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old_path, new_path)                       # STEP 1
            cls._commit_origin(image.id, new_origin)            # STEP 2 原子提交
            stats.images_migrated += 1
        elif not old_exists and new_exists:
            # 中断态：rename 已完成，DB 未 UPDATE。补 STEP 2。
            cls._commit_origin(image.id, new_origin)
            stats.images_recovered += 1
        else:
            # 两者都在：中断态，可能上次 rename 崩溃后残留（正常场景不出现，rename 是原子的）。
            # 保守走 STEP 2，best-effort 删 old。
            cls._commit_origin(image.id, new_origin)
            try:
                old_path.unlink()
            except OSError as exc:
                logger.warning(
                    "plot layout unlink old failed path={} detail={}",
                    old_path, exc,
                )
            stats.images_migrated += 1

        # STEP 3：尝试清理 old_path 所在的 plots/ 目录（幂等）；同一 movie 会被反复尝试
        # 但只有最后一个 image 处理完时才真正删掉，无需缓存。
        cls._prune_empty_plot_dir(old_path.parent, stats)

    @staticmethod
    def _commit_origin(image_id: int, new_origin: str) -> None:
        # origin/small/medium/large 保持一致（写入侧 _upsert_image_record 已统一为同一路径）。
        Image.update(
            origin=new_origin,
            small=new_origin,
            medium=new_origin,
            large=new_origin,
        ).where(Image.id == image_id).execute()

    @staticmethod
    def _prune_empty_plot_dir(plot_dir: Path, stats: MigrationStats) -> None:
        try:
            if plot_dir.is_dir() and not any(plot_dir.iterdir()):
                plot_dir.rmdir()
                stats.empty_dirs_pruned += 1
        except OSError:
            return

    @staticmethod
    def _image_root_path() -> Path:
        image_root = Path(settings.media.import_image_root_path).expanduser()
        if not image_root.is_absolute():
            image_root = (Path.cwd() / image_root).resolve()
        return image_root
