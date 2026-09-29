"""影片图片（封面/薄封面/剧情图）的打包重建。

包约定：``movies/<shard>/<番号>/assets.zip``，条目名 = 文件名。重建以数据库
活跃 origin 为准：字节优先取 loose 文件，缺失回退旧包条目；构建完成后复核
活跃集未变化才原子替换（规避与并发写入交错导致丢条目），成功后清理已入包的
loose 文件与历史临时包。活跃集为空时删除包。
"""

from __future__ import annotations

import os
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from loguru import logger

from src.common.image_store import write_pack
from src.common.media_paths import MOVIE_ASSETS_PACK_NAME, media_image_root_path
from src.model import Image

MAX_REBUILD_ATTEMPTS = 3


class MovieAssetPackService:
    @staticmethod
    def movie_asset_pack_path(movie_dir_relative: PurePosixPath | str) -> Path:
        return (
            media_image_root_path()
            / PurePosixPath(movie_dir_relative)
            / MOVIE_ASSETS_PACK_NAME
        )

    @classmethod
    def remove_movie_asset_pack(cls, movie_dir_relative: PurePosixPath | str) -> None:
        """删除影片资产包与遗留临时包；用于"稳定路径覆盖写"前先撤掉旧包。"""
        pack_path = cls.movie_asset_pack_path(movie_dir_relative)
        cls._cleanup_stale_temp_packs(pack_path)
        cls._remove_pack(pack_path)

    @classmethod
    def rebuild_movie_asset_pack(cls, movie_dir_relative: PurePosixPath | str) -> bool:
        """重建影片资产包；返回当前是否存在包。活跃集为空时删除包。"""
        movie_dir = PurePosixPath(movie_dir_relative)
        image_root = media_image_root_path()
        pack_path = image_root / movie_dir / MOVIE_ASSETS_PACK_NAME
        scope_dir = image_root / movie_dir

        for _attempt in range(MAX_REBUILD_ATTEMPTS):
            origins = cls.live_origins(movie_dir)
            if not origins:
                cls._remove_pack(pack_path)
                return False
            entries = cls._load_entries(pack_path, image_root, origins)
            if entries is None:
                # 有活跃行但拿不到字节（并发写入中间态）：重新快照后重试。
                continue
            cls._cleanup_stale_temp_packs(pack_path)
            tmp_path = pack_path.with_name(f"{pack_path.name}.tmp-{uuid.uuid4().hex}")
            try:
                write_pack(tmp_path, entries)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            if cls.live_origins(movie_dir) != origins:
                # 构建期间活跃集变化：丢弃本次构建重新来。
                tmp_path.unlink(missing_ok=True)
                continue
            os.replace(tmp_path, pack_path)
            cls._remove_loose_files(scope_dir, pack_path)
            return True

        logger.warning(
            "Rebuild movie asset pack skipped due to unstable live set dir={}",
            movie_dir,
        )
        return pack_path.is_file()

    @staticmethod
    def live_origins(movie_dir: PurePosixPath) -> list[str]:
        """影片目录直接子文件的图片 origin；media/ 等子目录（时间轴缩略图）不参与打包。"""
        prefix = f"{movie_dir.as_posix()}/"
        return [
            origin
            for (origin,) in Image.select(Image.origin)
            .where(Image.origin.startswith(prefix))
            .order_by(Image.origin.asc())
            .tuples()
            if origin.startswith(prefix) and "/" not in origin[len(prefix):]
        ]

    @staticmethod
    def _load_entries(
        pack_path: Path, image_root: Path, origins: list[str]
    ) -> list[tuple[str, bytes]] | None:
        old_archive: zipfile.ZipFile | None = None
        if pack_path.is_file():
            try:
                old_archive = zipfile.ZipFile(pack_path)
            except (zipfile.BadZipFile, OSError) as exc:
                logger.warning(
                    "Open movie asset pack failed pack={} detail={}", pack_path, exc
                )
        entries: list[tuple[str, bytes]] = []
        try:
            for origin in origins:
                entry_name = PurePosixPath(origin).name
                loose_path = image_root / origin
                if loose_path.is_file():
                    entries.append((entry_name, loose_path.read_bytes()))
                    continue
                if old_archive is not None:
                    try:
                        entries.append((entry_name, old_archive.read(entry_name)))
                        continue
                    except KeyError:
                        pass
                return None
            return entries
        finally:
            if old_archive is not None:
                old_archive.close()

    @staticmethod
    def _remove_pack(pack_path: Path) -> None:
        try:
            pack_path.unlink()
        except FileNotFoundError:
            return

    @staticmethod
    def _cleanup_stale_temp_packs(pack_path: Path) -> None:
        for stale in pack_path.parent.glob(f"{pack_path.name}.tmp-*"):
            try:
                stale.unlink()
            except OSError:
                continue

    @staticmethod
    def _remove_loose_files(scope_dir: Path, pack_path: Path) -> None:
        # 包已成权威：清理目录内平铺的图片单文件，保留包本身与包前缀的临时/备份文件。
        for entry in scope_dir.iterdir():
            if entry.name == pack_path.name or entry.name.startswith(
                f"{pack_path.name}."
            ):
                continue
            if entry.is_file() or entry.is_symlink():
                try:
                    entry.unlink()
                except OSError:
                    continue
