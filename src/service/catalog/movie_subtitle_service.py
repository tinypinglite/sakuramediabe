from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from src.api.exception.errors import ApiError
from src.common import build_signed_subtitle_url
from src.common.media_paths import movie_subtitle_dir
from src.common.service_helpers import require_record
from src.common.subtitle_paths import ensure_movie_subtitle_path
from src.model import Movie, Subtitle
from src.schema.catalog.subtitles import (
    MovieSubtitleItemResource,
    MovieSubtitleListResource,
    SubtitleAsset,
    SubtitleContent,
    SubtitleReadError,
)

MAX_SUBTITLE_CONTENT_BYTES = 10 * 1024 * 1024


class MovieSubtitleService:
    @staticmethod
    def _require_subtitle_movie(movie_id: int) -> Movie:
        movie = Movie.get_or_none(Movie.id == movie_id)
        if movie is None:
            raise SubtitleReadError("movie_not_found", "影片不存在")
        return movie

    @classmethod
    def list_subtitle_assets(cls, movie_id: int) -> tuple[SubtitleAsset, ...]:
        """纯读已登记字幕，跳过失效记录，不扫描、登记或清理文件。"""
        movie = cls._require_subtitle_movie(movie_id)
        items = []
        for subtitle in cls._subtitle_query(movie):
            try:
                path = ensure_movie_subtitle_path(movie, subtitle.file_path)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    continue
            except (ApiError, OSError, RuntimeError):
                continue
            items.append(SubtitleAsset(
                subtitle_id=subtitle.id,
                file_name=path.name,
                format=path.suffix.lower().lstrip("."),
                size_bytes=info.st_size,
                created_at=subtitle.created_at,
            ))
        return tuple(items)

    @classmethod
    def read_subtitle_content(cls, movie_id: int, subtitle_id: int) -> SubtitleContent:
        movie = cls._require_subtitle_movie(movie_id)
        subtitle = Subtitle.get_or_none(
            (Subtitle.id == subtitle_id) & (Subtitle.movie == movie.id)
        )
        if subtitle is None:
            raise SubtitleReadError("subtitle_not_found", "该影片下不存在此字幕")
        try:
            path = ensure_movie_subtitle_path(movie, subtitle.file_path)
        except (ApiError, RuntimeError):
            raise SubtitleReadError("subtitle_path_invalid", "字幕路径非法") from None
        except OSError:
            raise SubtitleReadError("subtitle_unavailable", "字幕文件不可访问") from None
        try:
            if not path.is_file():
                raise SubtitleReadError("subtitle_unavailable", "字幕文件不可访问")
            with path.open("rb") as file:
                info = os.fstat(file.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise SubtitleReadError("subtitle_unavailable", "字幕文件不可访问")
                if info.st_size > MAX_SUBTITLE_CONTENT_BYTES:
                    raise SubtitleReadError("subtitle_too_large", "字幕文件超过 10 MiB")
                # 文件读取期间可能增长，必须同时限制实际读取量。
                content = file.read(MAX_SUBTITLE_CONTENT_BYTES + 1)
        except OSError:
            raise SubtitleReadError("subtitle_unavailable", "字幕文件不可访问") from None
        if len(content) > MAX_SUBTITLE_CONTENT_BYTES:
            raise SubtitleReadError("subtitle_too_large", "字幕文件超过 10 MiB")
        return SubtitleContent(
            subtitle_id=subtitle.id,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        )

    @classmethod
    def get_movie_subtitles(cls, movie_number: str) -> MovieSubtitleListResource:
        movie = require_record(
            Movie,
            Movie.movie_number == movie_number,
            error_code="movie_not_found",
            error_message="影片不存在",
            error_details={"movie_number": movie_number},
        )
        # 读接口保持纯读，只返回当前仍然可访问的字幕项。
        items = cls._build_subtitle_items(movie)
        return MovieSubtitleListResource(
            movie_number=movie.movie_number,
            items=items,
        )

    @classmethod
    def sync_movie_subtitles(cls, movie: Movie) -> dict[str, int]:
        discovered_paths = cls._discover_subtitle_paths(movie)
        existing_items = list(cls._subtitle_query(movie))
        existing_by_path: dict[str, Subtitle] = {}
        deleted_count = 0

        # 先清理已经失效的字幕记录，避免后续列表继续暴露坏链接。
        for subtitle in existing_items:
            try:
                normalized_path = str(ensure_movie_subtitle_path(movie, subtitle.file_path))
            except ApiError:
                subtitle.delete_instance()
                deleted_count += 1
                continue
            if not Path(normalized_path).exists():
                subtitle.delete_instance()
                deleted_count += 1
                continue
            existing_by_path[normalized_path] = subtitle

        created_count = 0
        for subtitle_path in discovered_paths:
            key = str(subtitle_path)
            if key in existing_by_path:
                continue
            existing_by_path[key] = Subtitle.create(movie=movie, file_path=key)
            created_count += 1

        return {
            "created_subtitles": created_count,
            "deleted_subtitles": deleted_count,
            "total_subtitles": len(existing_by_path),
        }

    @staticmethod
    def _subtitle_query(movie: Movie):
        return (
            Subtitle.select(Subtitle)
            .where(Subtitle.movie == movie)
            .order_by(Subtitle.created_at.desc(), Subtitle.id.desc())
        )

    @classmethod
    def _build_subtitle_items(cls, movie: Movie) -> list[MovieSubtitleItemResource]:
        items: list[MovieSubtitleItemResource] = []
        for subtitle in cls._subtitle_query(movie):
            try:
                absolute_path = ensure_movie_subtitle_path(movie, subtitle.file_path)
            except ApiError:
                continue
            if not absolute_path.exists() or not absolute_path.is_file():
                continue
            items.append(
                MovieSubtitleItemResource(
                    subtitle_id=subtitle.id,
                    url=build_signed_subtitle_url(subtitle.id),
                    created_at=subtitle.created_at,
                    file_name=Path(absolute_path).name,
                )
            )
        return items

    @classmethod
    def _discover_subtitle_paths(cls, movie: Movie) -> list[Path]:
        """扫描该影片标准字幕目录下的 .srt 文件。"""
        scan_root = movie_subtitle_dir(movie.movie_number)
        if not scan_root.is_dir():
            return []
        discovered_paths: list[Path] = []
        for subtitle_path in sorted(scan_root.iterdir(), key=lambda item: item.name.lower()):
            if not subtitle_path.is_file() or subtitle_path.suffix.lower() != ".srt":
                continue
            try:
                discovered_paths.append(ensure_movie_subtitle_path(movie, subtitle_path))
            except ApiError:
                continue
        return discovered_paths
