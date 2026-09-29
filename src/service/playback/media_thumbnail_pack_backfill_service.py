"""媒体缩略图打包回填的手动维护服务。

存量 media 的缩略图是 ``thumbnails/<offset>.webp`` 单文件；本服务把它们回填成
``thumbnails.zip``（ZIP_STORED 容器，条目名 = 文件名）。回填以数据库为准，
缺文件时整条 media 跳过，不做静默丢弃。
"""

from __future__ import annotations

import os
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from loguru import logger

from src.common.image_store import write_pack
from src.common.media_paths import image_pack_relative_path, media_image_root_path
from src.model import Image, MediaThumbnail
from src.service.playback.operation_locks import (
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)


class MediaThumbnailPackBackfillService:
    """把存量单文件缩略图回填为包（手动任务）。"""

    TASK_KEY = "media_thumbnail_pack_backfill"

    @staticmethod
    def _candidate_media_ids() -> list[int]:
        return [
            int(media_id)
            for (media_id,) in (
                MediaThumbnail.select(MediaThumbnail.media)
                .distinct()
                .order_by(MediaThumbnail.media)
                .tuples()
            )
        ]

    @staticmethod
    def _media_thumbnail_rows(media_id: int) -> list[tuple[int, str]]:
        return [
            (int(offset), origin)
            for offset, origin in (
                MediaThumbnail.select(MediaThumbnail.offset, Image.origin)
                .join(Image)
                .where(MediaThumbnail.media == media_id)
                .order_by(MediaThumbnail.offset.asc())
                .tuples()
            )
        ]

    @staticmethod
    def _pack_covers_origins(pack_path: Path, origins: list[str]) -> bool:
        try:
            with zipfile.ZipFile(pack_path) as archive:
                entry_names = set(archive.namelist())
        except (zipfile.BadZipFile, OSError):
            return False
        return all(PurePosixPath(origin).name in entry_names for origin in origins)

    @staticmethod
    def _pack_matches_files(pack_path: Path, files: list[tuple[str, Path]]) -> bool:
        try:
            with zipfile.ZipFile(pack_path) as archive:
                sizes = {info.filename: info.file_size for info in archive.infolist()}
        except (zipfile.BadZipFile, OSError):
            return False
        return all(
            sizes.get(entry_name) == source.stat().st_size
            for entry_name, source in files
        )

    @staticmethod
    def _remove_legacy_thumbnail_files(thumbnails_dir: Path) -> None:
        for entry in thumbnails_dir.iterdir():
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
        try:
            thumbnails_dir.rmdir()
        except OSError:
            pass

    @classmethod
    def _process_media(cls, media_id: int, stats: dict[str, Any]) -> None:
        rows = cls._media_thumbnail_rows(media_id)
        if not rows:
            return
        image_root = media_image_root_path()
        origins = [origin for _, origin in rows]
        thumbnails_dir = image_root / PurePosixPath(origins[0]).parent
        pack_relative = image_pack_relative_path(origins[0])
        if pack_relative is None:
            raise ValueError("thumbnail_pack_path_unexpected")
        pack_path = image_root / pack_relative

        if pack_path.is_file():
            if not thumbnails_dir.is_dir():
                stats["already_packed_media"] += 1
                return
            if not cls._pack_covers_origins(pack_path, origins):
                logger.warning(
                    "Media thumbnail pack incomplete, keep legacy files media_id={} pack={}",
                    media_id,
                    pack_path,
                )
                stats["incomplete_media"] += 1
                return
            cls._remove_legacy_thumbnail_files(thumbnails_dir)
            stats["cleaned_media"] += 1
            return

        entries: list[tuple[str, Path]] = []
        for _, origin in rows:
            source = image_root / origin
            if not source.is_file() or source.stat().st_size <= 0:
                logger.warning(
                    "Media thumbnail legacy file missing media_id={} path={}",
                    media_id,
                    origin,
                )
                stats["skipped_missing_files"] += 1
                return
            entries.append((PurePosixPath(origin).name, source))

        tmp_path = pack_path.with_name(f"{pack_path.name}.tmp-{uuid.uuid4().hex}")
        try:
            write_pack(tmp_path, entries)
            if not cls._pack_matches_files(tmp_path, entries):
                raise RuntimeError("thumbnail_pack_self_check_failed")
            os.replace(tmp_path, pack_path)
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            logger.warning(
                "Media thumbnail pack backfill failed media_id={} detail={}",
                media_id,
                exc,
            )
            stats["failed_media"] += 1
            return
        cls._remove_legacy_thumbnail_files(thumbnails_dir)
        stats["packed_media"] += 1

    @classmethod
    def backfill(cls, *, reporter) -> dict[str, Any]:
        media_ids = cls._candidate_media_ids()
        stats: dict[str, Any] = {
            "candidate_media": len(media_ids),
            "packed_media": 0,
            "cleaned_media": 0,
            "already_packed_media": 0,
            "skipped_missing_files": 0,
            "incomplete_media": 0,
            "skipped_busy": 0,
            "failed_media": 0,
        }
        logger.info(
            "Media thumbnail pack backfill started candidate_media={}", len(media_ids)
        )
        step = max(len(media_ids) // 20, 1)

        def emit_progress(completed: int) -> None:
            reporter.emit(
                current=completed,
                total=len(media_ids),
                text=(
                    f"媒体缩略图打包回填 · 已完成 {completed}/{len(media_ids)}"
                    f" · 已打包 {stats['packed_media']}"
                    f" · 已清理 {stats['cleaned_media']}"
                    f" · 跳过 {stats['skipped_missing_files'] + stats['skipped_busy']}"
                    f" · 失败 {stats['failed_media']}"
                ),
                summary_patch=stats,
            )

        emit_progress(0)
        for completed, media_id in enumerate(media_ids, start=1):
            if completed == 1 or completed % step == 0:
                logger.info(
                    "Media thumbnail pack backfill progress completed={}/{} packed={} cleaned={} skipped_missing={} skipped_busy={} failed={}",
                    completed,
                    len(media_ids),
                    stats["packed_media"],
                    stats["cleaned_media"],
                    stats["skipped_missing_files"],
                    stats["skipped_busy"],
                    stats["failed_media"],
                )
            reporter.emit(
                current=completed - 1,
                total=len(media_ids),
                text=(
                    f"媒体缩略图打包回填 · 正在处理 {completed}/{len(media_ids)}"
                    f" · 已打包 {stats['packed_media']} · 失败 {stats['failed_media']}"
                ),
                summary_patch=stats,
            )
            try:
                with media_operation_lock(MEDIA_LOCK, media_id):
                    cls._process_media(media_id, stats)
            except MediaOperationBusy:
                stats["skipped_busy"] += 1
            except Exception as exc:
                stats["failed_media"] += 1
                logger.warning(
                    "Media thumbnail pack backfill failed media_id={} detail={}",
                    media_id,
                    exc,
                )
            emit_progress(completed)
        return stats
