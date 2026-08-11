"""字幕资产写入的稳定操作集合。

目录导入与插件共用同一实现：查影片 → 扩展名校验 → 内容指纹去重 →
落 ``movies/<shard>/<番号>/subtitles/`` → 登记 Subtitle 行。
插件通过 ``PluginContext.import_subtitle`` 调用，不直接触碰路径与登记细节。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from loguru import logger

from src.common.media_paths import MOVIE_SUBTITLE_EXTENSIONS
from src.common.service_helpers import find_movie_by_number
from src.common.subtitle_paths import ensure_movie_subtitle_path
from src.model import Subtitle
from src.schema.catalog.subtitles import (
    SubtitleImportResult,
    SubtitleImportStatus,
)
from src.service.transfers.imports.writer import prepare_movie_subtitle_target_path
from src.service.transfers.shared.file_transfer import transfer_file


class SubtitleAssetService:
    """字幕资产写入/登记的唯一实现入口。"""

    @classmethod
    def movie_subtitle_hashes(cls, movie) -> set[str]:
        """该影片已登记字幕的内容指纹集合（含新老布局合法路径）。"""
        hashes: set[str] = set()
        for subtitle in Subtitle.select().where(Subtitle.movie == movie):
            try:
                absolute_path = ensure_movie_subtitle_path(movie, subtitle.file_path)
            except Exception as exc:
                logger.warning(
                    "Subtitle path invalid movie_id={} subtitle_id={} detail={}",
                    movie.id,
                    subtitle.id,
                    exc,
                )
                continue
            if absolute_path.is_file():
                hashes.add(cls._sha256_file(absolute_path))
        return hashes

    @classmethod
    def import_subtitle_content(
        cls,
        movie_number: str,
        content: bytes,
        filename: str,
        language: str | None = None,
    ) -> SubtitleImportResult:
        """按番号写入一段字幕内容（插件下载场景）。"""
        del language  # Subtitle 模型暂无语言列，参数保留供后续版本使用。
        movie = find_movie_by_number(movie_number)
        if movie is None:
            return SubtitleImportResult(
                status=SubtitleImportStatus.MOVIE_NOT_FOUND,
                reason=f"影片不存在: {movie_number}",
            )

        suffix = Path(filename or "").suffix.lower()
        if suffix not in MOVIE_SUBTITLE_EXTENSIONS:
            return SubtitleImportResult(
                status=SubtitleImportStatus.INVALID_FORMAT,
                reason=f"不支持的扩展名: {suffix or '无'}（支持 {', '.join(MOVIE_SUBTITLE_EXTENSIONS)}）",
            )

        content_hash = cls._sha256_bytes(content)
        if content_hash in cls.movie_subtitle_hashes(movie):
            return SubtitleImportResult(status=SubtitleImportStatus.DUPLICATE)

        target_path = prepare_movie_subtitle_target_path(
            movie.movie_number, None, extension=suffix
        )
        cls._write_atomic(target_path, content)
        subtitle = Subtitle.create(movie=movie, file_path=str(target_path))
        return SubtitleImportResult(
            status=SubtitleImportStatus.IMPORTED,
            subtitle_id=subtitle.id,
        )

    @classmethod
    def register_subtitle_file(
        cls,
        movie,
        source_path: Path,
        *,
        existing_hashes: dict[int, set[str]] | None = None,
        transfer_mode: str = "auto",
    ) -> tuple[str, str, str]:
        """登记一个本地字幕文件（目录导入场景），返回 (status, reason, detail)。"""
        content_hash = cls._sha256_file(source_path)
        hashes = existing_hashes.get(movie.id) if existing_hashes else None
        if hashes is None:
            hashes = cls.movie_subtitle_hashes(movie)
            if existing_hashes is not None:
                existing_hashes[movie.id] = hashes
        if content_hash in hashes:
            return "skipped", "duplicate_fingerprint", source_path.name

        target_path = prepare_movie_subtitle_target_path(
            movie.movie_number,
            None,
            extension=source_path.suffix.lower(),
        )
        transfer_file(source_path, target_path, transfer_mode=transfer_mode)
        Subtitle.create(movie=movie, file_path=str(target_path))

        hashes.add(content_hash)
        return "imported", "", str(target_path)

    @staticmethod
    def _write_atomic(target_path: Path, content: bytes) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
        tmp_path.write_bytes(content)
        os.replace(tmp_path, target_path)

    @staticmethod
    def _sha256_file(file_path: Path) -> str:
        digest = hashlib.sha256()
        with open(file_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _sha256_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()
