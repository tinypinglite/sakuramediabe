from __future__ import annotations

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
)


class MovieSubtitleService:
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
