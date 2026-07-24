"""影片资产目录分片迁移：把 ``movies/<番号>/`` 迁到 ``movies/<sha1(番号)[:2]>/<番号>/``。

用户通过 CLI ``python -m src.start.commands migrate-movie-asset-shard`` 手动触发；不进 lifespan。

动机：``movies/`` 是单层平铺目录，一个番号一个子目录。番号规模到 30w 时，htree 查单条尚可，
但 ``readdir`` 是 O(n)，``ls`` / ``du`` / ``rsync`` / ``tar`` 备份 / docker 卷迁移全部退化。
按 sha1 前 2 位分成固定 256 片后，顶层条目数恒为 256。

设计核心 —— 两阶段：

    Phase 1  文件系统归片   os.rename(movies/<num>, movies/<shard>/<num>)   同 fs 内原子
    Phase 2  DB 前缀重写    image.origin/small/medium/large 按 keyset 分页批量 UPDATE

崩溃安全靠幂等，不靠事务：

- **文件系统侧幂等**：已归片的番号目录不再出现在 ``movies/`` 顶层，重跑天然跳过；
- **DB 侧幂等**：``image.origin`` 前缀重写只命中老布局行，已分片的行 ``legacy_asset_dir_name``
  返回 None 天然跳过。

Ctrl+C / 掉电后重跑即收敛：Phase 1 只处理剩下没搬完的，Phase 2 全表重扫但只写老布局行。
文件先 rename 落位、origin 后重写，任意中断点重跑都能对齐到"文件在哪 origin 就指哪"。

前提：番号归一化后不会等于任一分片名（两位小写十六进制）。真实 javdb 番号是"厂牌前缀 + 数字"，
含大写、长于 2 字符，恒不命中，因此顶层目录名落在 256 个分片名集合内即视为分片目录、不做冲突校验。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from loguru import logger

from src.common.media_paths import (
    MOVIE_ASSETS_SUBDIR,
    MOVIE_ASSET_SHARD_NAMES,
    media_image_root_path,
    movie_asset_shard,
)
from src.model import Image


# 迁移进度回调：(phase, current, total)；total 为 -1 表示总量未知（文件系统阶段边扫边搬）。
ProgressCallback = Callable[[str, int, int], None]

PHASE_DIRECTORIES = "directories"
PHASE_IMAGES = "images"


@dataclass
class MigrationStats:
    # Phase 1 文件系统
    directories_scanned: int = 0
    directories_sharded: int = 0            # 正常 rename 归片
    directories_merged: int = 0             # 目标已存在，逐项并入后删空源目录
    directories_conflict_skipped: int = 0   # 并入时有同名残留，源目录保留在顶层交人工处理
    unexpected_root_entries: int = 0        # movies/ 顶层的非目录条目，不动
    # Phase 2 DB
    images_scanned: int = 0
    images_rewritten: int = 0
    images_conflict_skipped: int = 0        # 目标 origin 已被别的行占用（unique 约束）
    conflict_origins: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "directories_scanned": self.directories_scanned,
            "directories_sharded": self.directories_sharded,
            "directories_merged": self.directories_merged,
            "directories_conflict_skipped": self.directories_conflict_skipped,
            "unexpected_root_entries": self.unexpected_root_entries,
            "images_scanned": self.images_scanned,
            "images_rewritten": self.images_rewritten,
            "images_conflict_skipped": self.images_conflict_skipped,
            "conflict_origins": list(self.conflict_origins),
        }


def legacy_asset_dir_name(origin: str) -> str | None:
    """老布局 ``image.origin`` -> 番号目录名；已分片或不是影片资产返回 ``None``。

    老布局 ``movies/<num>/...``       至少 3 段
    新布局 ``movies/<shard>/<num>/…`` 至少 4 段，且 shard 与 num 的哈希自洽

    自洽校验（``movie_asset_shard(parts[2]) == parts[1]``）保证判定不依赖段数猜测：
    番号目录名不可能是分片名（真实 javdb 番号恒不等于两位小写十六进制）。
    """
    parts = origin.split("/")
    if len(parts) < 3 or parts[0] != MOVIE_ASSETS_SUBDIR:
        return None
    if (
        parts[1] in MOVIE_ASSET_SHARD_NAMES
        and len(parts) >= 4
        and movie_asset_shard(parts[2]) == parts[1]
    ):
        return None
    return parts[1]


class MovieAssetShardMigrationService:
    """把 ``movies/`` 下的平铺番号目录归到 256 个 sha1 分片，并重写 ``image.origin``。"""

    # 每轮从 image 表按主键取多少行；纯 PK 索引扫描，不走 LIKE。
    SCAN_BATCH_SIZE = 5000
    # 单条 bulk_update 覆盖多少行；peewee 会展开成 CASE WHEN，太大反而拖慢。
    UPDATE_BATCH_SIZE = 500

    @classmethod
    def run(
        cls,
        *,
        dry_run: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> MigrationStats:
        """迁移入口。``dry_run`` 只统计不动任何文件与数据库。"""
        stats = MigrationStats()
        movies_root = media_image_root_path() / MOVIE_ASSETS_SUBDIR
        if movies_root.is_dir():
            cls._shard_directories(movies_root, stats, dry_run=dry_run, progress=progress_callback)
        else:
            # 全新安装：图片根还没建过 movies/，没有存量可迁；DB 侧照样跑一遍（空表秒退）。
            logger.info("movie asset shard skip filesystem phase, movies root absent path={}", movies_root)
        cls._rewrite_image_origins(stats, dry_run=dry_run, progress=progress_callback)
        return stats

    # ---- Phase 1：文件系统归片 ----

    @classmethod
    def _shard_directories(
        cls,
        movies_root: Path,
        stats: MigrationStats,
        *,
        dry_run: bool,
        progress: ProgressCallback | None,
    ) -> None:
        if not dry_run:
            # 先把 256 个分片目录建满，之后 scandir 时"哪些名字是分片"就是确定集合。
            for shard_name in sorted(MOVIE_ASSET_SHARD_NAMES):
                (movies_root / shard_name).mkdir(exist_ok=True)

        # rename 只从被遍历目录里移除条目、不新增；再套一层"直到某轮没搬动"，
        # 消除 readdir 在自身被修改时可能漏读条目的风险。
        # 注意：`progressed_in_round` 只统计"真正把源目录清出顶层"的操作，merge 冲突残留
        # （源目录搬不空、无法 rmdir）不算推进；否则同一残留目录会在每轮 scandir 里重复出现，
        # 循环永远退不出来。已处理过但无法推进的源目录名记入 stalled_names，下一轮直接跳过。
        stalled_names: set[str] = set()
        while True:
            progressed_in_round = 0
            with os.scandir(movies_root) as entries:
                for entry in entries:
                    if entry.name in MOVIE_ASSET_SHARD_NAMES:
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        # movies/ 顶层出现散文件/符号链接，不属于任何番号，保持原样只记账。
                        # 已计过的条目下一轮无需重复告警，同样记入 stalled_names 短路。
                        if entry.name not in stalled_names:
                            stats.unexpected_root_entries += 1
                            logger.warning("movie asset shard unexpected root entry path={}", entry.path)
                            stalled_names.add(entry.name)
                        continue
                    if entry.name in stalled_names:
                        # 上一轮已判定无法推进（merge 冲突残留），本轮直接跳过避免重复处理。
                        continue

                    stats.directories_scanned += 1
                    if dry_run:
                        stats.directories_sharded += 1
                        progressed_in_round += 1
                    else:
                        moved = cls._shard_one_directory(
                            movies_root, Path(entry.path), entry.name, stats,
                        )
                        if moved:
                            progressed_in_round += 1
                        else:
                            # merge 冲突残留：本轮源目录留在顶层，标记为已知无解，
                            # 后续轮次不再重复扫入避免无限循环。
                            stalled_names.add(entry.name)
                    if progress is not None:
                        progress(PHASE_DIRECTORIES, stats.directories_scanned, -1)

            if dry_run or progressed_in_round == 0:
                break

    @classmethod
    def _shard_one_directory(
        cls,
        movies_root: Path,
        source: Path,
        dir_name: str,
        stats: MigrationStats,
    ) -> bool:
        """把单个番号目录搬进分片；返回是否真正把源目录清出了顶层。

        返回 False 表示源目录仍留在顶层（merge 后 rmdir 失败），调用方据此不再重复扫入。
        """
        target = movies_root / movie_asset_shard(dir_name) / dir_name
        if not target.exists():
            os.rename(source, target)          # 同一文件系统内的目录改名，天然原子
            stats.directories_sharded += 1
            return True
        # 目标已存在：只可能是历史残留（rename 本身原子，正常流程不会两边都在）。
        return cls._merge_directory(source, target, stats)

    @staticmethod
    def _merge_directory(source: Path, target: Path, stats: MigrationStats) -> bool:
        """把源目录内容逐项并入已存在的目标目录，再删空源目录；返回是否成功删掉源目录。

        同名条目两边都有时保留目标侧（已归片的一侧是权威），源侧条目原样留下不删，
        源目录因此非空、无法 rmdir，会留在顶层并计入 conflict，交人工判断。
        """
        for child in source.iterdir():
            destination = target / child.name
            if destination.exists():
                logger.warning(
                    "movie asset shard merge conflict kept target src={} dst={}",
                    child, destination,
                )
                continue
            os.rename(child, destination)
        try:
            source.rmdir()
        except OSError:
            stats.directories_conflict_skipped += 1
            logger.warning("movie asset shard merge left residue src={} dst={}", source, target)
            return False
        stats.directories_merged += 1
        return True

    # ---- Phase 2：image.origin 前缀重写 ----

    @classmethod
    def _rewrite_image_origins(
        cls,
        stats: MigrationStats,
        *,
        dry_run: bool,
        progress: ProgressCallback | None,
    ) -> None:
        total = Image.select(Image.id).count()
        last_id = 0
        while True:
            rows = list(
                Image.select(Image.id, Image.origin)
                .where(Image.id > last_id)
                .order_by(Image.id.asc())
                .limit(cls.SCAN_BATCH_SIZE)
            )
            if not rows:
                break
            last_id = rows[-1].id
            stats.images_scanned += len(rows)

            rewrites = cls._plan_batch_rewrites(rows, stats)
            if rewrites and not dry_run:
                Image.bulk_update(
                    rewrites,
                    fields=[Image.origin, Image.small, Image.medium, Image.large],
                    batch_size=cls.UPDATE_BATCH_SIZE,
                )
            stats.images_rewritten += len(rewrites)
            if progress is not None:
                progress(PHASE_IMAGES, stats.images_scanned, total)

    @classmethod
    def _plan_batch_rewrites(cls, rows: Iterable[Image], stats: MigrationStats) -> list[Image]:
        """算出这一批里需要改写的行，并按 unique(origin) 过滤掉会撞车的。"""
        candidates: list[tuple[Image, str]] = []
        for image in rows:
            legacy_dir_name = legacy_asset_dir_name(image.origin)
            if legacy_dir_name is None:
                continue
            shard_name = movie_asset_shard(legacy_dir_name)
            suffix = image.origin[len(MOVIE_ASSETS_SUBDIR) + 1:]
            candidates.append((image, f"{MOVIE_ASSETS_SUBDIR}/{shard_name}/{suffix}"))

        if not candidates:
            return []

        occupied = cls._occupied_origins([new_origin for _, new_origin in candidates])
        rewrites: list[Image] = []
        planned: set[str] = set()
        for image, new_origin in candidates:
            if new_origin in occupied or new_origin in planned:
                # origin 有 unique 约束：目标已被别的行占用，跳过并记账，不让 IntegrityError
                # 炸掉整个迁移事务（口径与 PlotLayoutMigrationService 一致）。
                stats.images_conflict_skipped += 1
                stats.conflict_origins.append(image.origin)
                logger.warning(
                    "movie asset shard origin conflict image_id={} old={} new={}",
                    image.id, image.origin, new_origin,
                )
                continue
            planned.add(new_origin)
            image.origin = new_origin
            image.small = new_origin
            image.medium = new_origin
            image.large = new_origin
            rewrites.append(image)
        return rewrites

    @classmethod
    def _occupied_origins(cls, new_origins: list[str]) -> set[str]:
        """一次性查出这批目标 origin 里已经被占用的，避免逐行查表。"""
        occupied: set[str] = set()
        for start in range(0, len(new_origins), cls.UPDATE_BATCH_SIZE):
            chunk = new_origins[start:start + cls.UPDATE_BATCH_SIZE]
            occupied.update(
                row.origin
                for row in Image.select(Image.origin).where(Image.origin.in_(chunk))
            )
        return occupied
