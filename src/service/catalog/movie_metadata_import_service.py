"""按番号搜索元数据插件并导入影片，使用 SSE 输出进度。"""

from collections.abc import Iterator

from loguru import logger

from src.common import normalize_movie_number
from src.common.service_helpers import find_movie_by_number
from src.metadata.provider import MetadataNotFoundError
from src.model import Movie
from src.schema.catalog.movies import MovieListItemResource
from src.service.catalog.catalog_import_service import (
    CatalogImportService,
    ImageDownloadError,
)
from src.service.catalog.metadata_source_service import (
    MetadataSourceError,
    MetadataSourceService,
)
from src.service.catalog.movie_list_media_service import attach_movie_list_media
from src.service.catalog.movie_service import MovieService


class MovieMetadataImportService:
    @staticmethod
    def _build_catalog_import_service() -> CatalogImportService:
        return CatalogImportService()

    @classmethod
    def stream_search_and_upsert_movie(
        cls,
        movie_number: str,
    ) -> Iterator[tuple[str, dict]]:
        normalized_movie_number = normalize_movie_number(movie_number)
        yield "search_started", {"movie_number": normalized_movie_number}

        if not normalized_movie_number:
            yield "completed", {
                "success": False,
                "reason": "movie_number_not_found",
                "movies": [],
            }
            return

        existing = find_movie_by_number(movie_number)
        if existing is not None:
            movie = MovieService.movie_list_query().where(Movie.id == existing.id).get()
            attach_movie_list_media([movie])
            yield "completed", {
                "success": True,
                "movies": [
                    MovieListItemResource.from_attributes_model(movie).model_dump(
                        exclude={"can_play"}
                    )
                ],
                "failed_items": [],
                "stats": {
                    "total": 1,
                    "created_count": 0,
                    "already_exists_count": 1,
                    "failed_count": 0,
                },
            }
            return

        try:
            with MetadataSourceService.fetch(normalized_movie_number) as outcome:
                detail = outcome.detail
                source = outcome.source
                source_errors = [item.as_dict() for item in outcome.source_errors]
                yield "movie_found", {
                    "movies": [
                        {
                            "movie_number": detail.movie_number,
                            "title": detail.title,
                            "source_name": source.get("display_name"),
                        }
                    ],
                    "total": 1,
                    "source_errors": source_errors,
                }
                yield "upsert_started", {"total": 1}

                stats = {
                    "total": 1,
                    "created_count": 0,
                    "already_exists_count": 0,
                    "failed_count": 0,
                }
                failed_items: list[dict[str, str]] = []
                imported_movies: list[MovieListItemResource] = []
                try:
                    service = cls._build_catalog_import_service()
                    movie, created = service.import_movie(detail, source)
                    movie_with_cover = (
                        MovieService.movie_list_query()
                        .where(Movie.id == movie.id)
                        .get_or_none()
                        or movie
                    )
                    attach_movie_list_media([movie_with_cover])
                    imported_movies.append(
                        MovieListItemResource.from_attributes_model(movie_with_cover)
                    )
                    stats[
                        "created_count" if created else "already_exists_count"
                    ] += 1
                except ImageDownloadError as exc:
                    stats["failed_count"] += 1
                    failed_items.append(
                        {
                            "movie_number": normalized_movie_number,
                            "reason": "image_download_failed",
                            "detail": str(exc),
                        }
                    )
                except Exception as exc:
                    stats["failed_count"] += 1
                    logger.exception(
                        "Movie metadata import failed movie_number={} detail={}",
                        normalized_movie_number,
                        exc,
                    )
                    failed_items.append(
                        {
                            "movie_number": normalized_movie_number,
                            "reason": "upsert_failed",
                            "detail": str(exc),
                        }
                    )

                yield "upsert_finished", stats
                if imported_movies:
                    yield "completed", {
                        "success": True,
                        "movies": [
                            item.model_dump(exclude={"can_play"})
                            for item in imported_movies
                        ],
                        "failed_items": failed_items,
                        "source_errors": source_errors,
                        "stats": stats,
                    }
                    return
                yield "completed", {
                    "success": False,
                    "reason": "internal_error",
                    "movies": [],
                    "failed_items": failed_items,
                    "source_errors": source_errors,
                    "stats": stats,
                }
        except MetadataNotFoundError:
            yield "completed", {
                "success": False,
                "reason": "movie_not_found",
                "movies": [],
            }
        except MetadataSourceError as exc:
            logger.warning(
                "Movie metadata sources failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            yield "completed", {
                "success": False,
                "reason": "metadata_fetch_failed",
                "movies": [],
                "source_errors": [item.as_dict() for item in exc.failures],
            }
        except Exception as exc:
            logger.exception(
                "Movie metadata search failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            yield "completed", {
                "success": False,
                "reason": "internal_error",
                "movies": [],
            }
