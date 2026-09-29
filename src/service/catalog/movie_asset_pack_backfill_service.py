"""影片图片（封面/薄封面/剧情图）打包回填的手动维护服务。

存量影片图片是平铺在 ``movies/<shard>/<番号>/`` 下的单文件；本服务把它们回填成
同目录的 ``assets.zip``（ZIP_STORED 容器，条目名 = 文件名）。回填以数据库为准，
缺文件时整片跳过，不做静默丢弃；已有包时清理残留单文件。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from src.common.media_paths import (
    MOVIE_ASSETS_PACK_NAME,
    media_image_root_path,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.model import Movie, MoviePlotImage
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService


class MovieAssetPackBackfillService:
    """把存量影片图片回填为包（手动任务）。"""

    TASK_KEY = "movie_asset_pack_backfill"

    @staticmethod
    def _candidate_movie_numbers() -> list[str]:
        with_cover = {
            row.movie_number
            for row in Movie.select(Movie.movie_number).where(
                Movie.cover_image.is_null(False)
            )
        }
        with_thin = {
            row.movie_number
            for row in Movie.select(Movie.movie_number).where(
                Movie.thin_cover_image.is_null(False)
            )
        }
        with_plots = {
            row[0]
            for row in (
                MoviePlotImage.select(Movie.movie_number)
                .join(Movie)
                .distinct()
                .tuples()
            )
        }
        return sorted(with_cover | with_thin | with_plots)

    @staticmethod
    def _loose_files(scope_dir: Path, pack_path: Path) -> list[Path]:
        if not scope_dir.is_dir():
            return []
        return [
            entry
            for entry in scope_dir.iterdir()
            if entry.is_file()
            and entry.name != pack_path.name
            and not entry.name.startswith(f"{pack_path.name}.")
        ]

    @classmethod
    def _process_movie(cls, movie_number: str, stats: dict[str, Any]) -> None:
        movie_dir = movie_asset_relative_dir(normalize_asset_dir_name(movie_number))
        image_root = media_image_root_path()
        scope_dir = image_root / movie_dir
        pack_path = scope_dir / MOVIE_ASSETS_PACK_NAME
        origins = MovieAssetPackService.live_origins(movie_dir)
        if not origins:
            return

        had_pack = pack_path.is_file()
        loose_files = cls._loose_files(scope_dir, pack_path)
        if had_pack and not loose_files:
            # 已打包且无残留，直接跳过（不回读包做逐条校验，避免大库重复开销）。
            stats["already_packed_movies"] += 1
            return

        if not had_pack:
            missing = [
                origin for origin in origins if not (image_root / origin).is_file()
            ]
            if missing:
                logger.warning(
                    "Movie asset legacy file missing movie_number={} missing={}",
                    movie_number,
                    len(missing),
                )
                stats["skipped_missing_files"] += 1
                return

        try:
            MovieAssetPackService.rebuild_movie_asset_pack(movie_dir)
        except Exception as exc:
            stats["failed_movies"] += 1
            logger.warning(
                "Movie asset pack backfill failed movie_number={} detail={}",
                movie_number,
                exc,
            )
            return
        if not pack_path.is_file():
            stats["failed_movies"] += 1
            logger.warning(
                "Movie asset pack backfill produced no pack movie_number={}",
                movie_number,
            )
            return
        if had_pack:
            stats["cleaned_movies"] += 1
        else:
            stats["packed_movies"] += 1

    @classmethod
    def backfill(cls, *, reporter) -> dict[str, Any]:
        movie_numbers = cls._candidate_movie_numbers()
        stats: dict[str, Any] = {
            "candidate_movies": len(movie_numbers),
            "packed_movies": 0,
            "cleaned_movies": 0,
            "already_packed_movies": 0,
            "skipped_missing_files": 0,
            "failed_movies": 0,
        }
        logger.info(
            "Movie asset pack backfill started candidate_movies={}",
            len(movie_numbers),
        )
        step = max(len(movie_numbers) // 20, 1)

        def emit_progress(completed: int) -> None:
            reporter.emit(
                current=completed,
                total=len(movie_numbers),
                text=(
                    f"影片图片打包回填 · 已完成 {completed}/{len(movie_numbers)}"
                    f" · 已打包 {stats['packed_movies']}"
                    f" · 已清理 {stats['cleaned_movies']}"
                    f" · 跳过 {stats['skipped_missing_files']}"
                    f" · 失败 {stats['failed_movies']}"
                ),
                summary_patch=stats,
            )

        emit_progress(0)
        for completed, movie_number in enumerate(movie_numbers, start=1):
            if completed == 1 or completed % step == 0:
                logger.info(
                    "Movie asset pack backfill progress completed={}/{} packed={} cleaned={} skipped_missing={} failed={}",
                    completed,
                    len(movie_numbers),
                    stats["packed_movies"],
                    stats["cleaned_movies"],
                    stats["skipped_missing_files"],
                    stats["failed_movies"],
                )
            reporter.emit(
                current=completed - 1,
                total=len(movie_numbers),
                text=(
                    f"影片图片打包回填 · 正在处理 {completed}/{len(movie_numbers)}"
                    f" · 已打包 {stats['packed_movies']} · 失败 {stats['failed_movies']}"
                ),
                summary_patch=stats,
            )
            try:
                cls._process_movie(movie_number, stats)
            except Exception as exc:
                stats["failed_movies"] += 1
                logger.warning(
                    "Movie asset pack backfill failed movie_number={} detail={}",
                    movie_number,
                    exc,
                )
            emit_progress(completed)
        return stats
