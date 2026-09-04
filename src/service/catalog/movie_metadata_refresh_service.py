"""影片元数据刷新与 JavDB 流式导入 service。

从 ``MovieService`` 拆出所有与远端 JavDB 交互并写回 Movie 记录的入口：
- ``refresh_movie_metadata``：强校验版元数据刷新；
- ``stream_search_and_upsert_movie_from_javdb``：单番号搜索并落库（SSE）；
- ``stream_import_series_movies_from_javdb``：按系列批量拉详情并落库（SSE）。
"""

from collections.abc import Iterator

from loguru import logger

from src.api.exception.errors import ApiError
from src.common import normalize_movie_number
from src.common.service_helpers import find_movie_by_number
from src.metadata._providers.models import JavdbMovieDetailResource
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Movie, MovieSeries
from src.schema.catalog.movies import MovieDetailResource, MovieListItemResource
from src.service.catalog.catalog_import_service import (
    CatalogImportService,
    ImageDownloadError,
)
from src.service.catalog.metadata_source_service import MetadataSourceService
from src.service.catalog.movie_service import MovieService


class MovieMetadataRefreshService:
    """封装影片元数据刷新与 JavDB 流式导入。"""

    @classmethod
    def _build_catalog_import_service(cls) -> CatalogImportService:
        return CatalogImportService()

    @staticmethod
    def _build_movie_metadata_refresh_error_details(
        *,
        movie_number: str,
        normalized_movie_number: str,
        detail: str,
    ) -> dict[str, str]:
        return {
            "movie_number": movie_number,
            "normalized_movie_number": normalized_movie_number,
            "detail": detail,
        }

    @classmethod
    def _raise_movie_metadata_refresh_failed(
        cls,
        *,
        movie_number: str,
        normalized_movie_number: str,
        exc: Exception,
        log_message: str | None = None,
    ) -> None:
        if log_message:
            logger.exception(
                log_message,
                movie_number,
                normalized_movie_number,
                exc,
            )
        raise ApiError(
            502,
            "movie_metadata_refresh_failed",
            "影片元数据刷新失败",
            cls._build_movie_metadata_refresh_error_details(
                movie_number=movie_number,
                normalized_movie_number=normalized_movie_number,
                detail=str(exc),
            ),
        ) from exc

    @classmethod
    def _fetch_remote_movie_metadata(
        cls,
        *,
        movie: Movie,
        normalized_movie_number: str,
    ) -> JavdbMovieDetailResource:
        try:
            return build_javdb_provider().get_movie_by_number(normalized_movie_number)
        except MetadataNotFoundError as exc:
            raise ApiError(
                404,
                "movie_metadata_not_found",
                "影片远端元数据不存在",
                {"movie_number": movie.movie_number, "normalized_movie_number": normalized_movie_number},
            ) from exc
        except MetadataRequestError as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=normalized_movie_number,
                exc=exc,
            )
        except Exception as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=normalized_movie_number,
                exc=exc,
                log_message="Movie metadata fetch failed movie_number={} normalized={} detail={}",
            )

    @classmethod
    def _validate_remote_movie_metadata_number(
        cls,
        *,
        movie: Movie,
        detail: JavdbMovieDetailResource,
    ) -> str:
        local_normalized_movie_number = normalize_movie_number(movie.movie_number)
        remote_normalized_movie_number = normalize_movie_number(detail.movie_number)
        # 严格要求远端详情与本地影片指向同一番号，避免误把相邻作品覆盖到当前记录。
        if not remote_normalized_movie_number or remote_normalized_movie_number != local_normalized_movie_number:
            raise ApiError(
                409,
                "movie_metadata_number_conflict",
                "远端元数据番号与本地影片不一致",
                {
                    "movie_number": movie.movie_number,
                    "normalized_movie_number": local_normalized_movie_number,
                    "remote_movie_number": detail.movie_number,
                    "remote_normalized_movie_number": remote_normalized_movie_number,
                },
            )
        return local_normalized_movie_number

    @classmethod
    def _validate_remote_movie_metadata_javdb_id(
        cls,
        *,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        normalized_movie_number: str,
    ) -> None:
        remote_javdb_id = (detail.javdb_id or "").strip()
        current_javdb_id = (movie.javdb_id or "").strip()
        if not remote_javdb_id or remote_javdb_id == current_javdb_id:
            return

        conflicting_movie = (
            Movie.select(Movie.movie_number)
            .where(
                (Movie.javdb_id == remote_javdb_id)
                & (Movie.id != movie.id)
            )
            .get_or_none()
        )
        if conflicting_movie is None:
            return

        # 远端主键已被其他本地影片占用时直接拒绝刷新，避免覆盖错片。
        raise ApiError(
            409,
            "movie_metadata_javdb_id_conflict",
            "远端元数据 JavDB ID 与其他本地影片冲突",
            {
                "movie_number": movie.movie_number,
                "normalized_movie_number": normalized_movie_number,
                "current_javdb_id": current_javdb_id,
                "remote_javdb_id": remote_javdb_id,
                "conflicting_movie_number": conflicting_movie.movie_number,
            },
        )

    @classmethod
    def refresh_movie_metadata(cls, movie_number: str) -> MovieDetailResource:
        movie, normalized_movie_number = MovieService.require_movie_by_normalized_number(movie_number)
        detail = cls._fetch_remote_movie_metadata(
            movie=movie,
            normalized_movie_number=normalized_movie_number,
        )
        local_normalized_movie_number = cls._validate_remote_movie_metadata_number(
            movie=movie,
            detail=detail,
        )
        cls._validate_remote_movie_metadata_javdb_id(
            movie=movie,
            detail=detail,
            normalized_movie_number=local_normalized_movie_number,
        )

        try:
            service = cls._build_catalog_import_service()
            refreshed_movie = (
                service.backfill_plugin_movie(movie, detail)
                if not movie.javdb_id and movie.metadata_source
                else service.refresh_movie_metadata_strict(movie, detail)
            )
        except ImageDownloadError as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=local_normalized_movie_number,
                exc=exc,
            )
        except Exception as exc:
            cls._raise_movie_metadata_refresh_failed(
                movie_number=movie.movie_number,
                normalized_movie_number=local_normalized_movie_number,
                exc=exc,
                log_message="Movie metadata refresh failed movie_number={} normalized={} detail={}",
            )

        return MovieService.get_movie_detail(refreshed_movie.movie_number)

    @classmethod
    def stream_search_and_upsert_movie_from_javdb(
        cls,
        movie_number: str,
    ) -> Iterator[tuple[str, dict]]:
        """按 SSE 事件顺序输出影片搜索和导入进度。"""
        normalized_movie_number = normalize_movie_number(movie_number)
        yield "search_started", {"movie_number": normalized_movie_number}

        if not normalized_movie_number:
            yield "completed", {"success": False, "reason": "movie_number_not_found", "movies": []}
            return

        existing = find_movie_by_number(movie_number)
        if existing is not None and not existing.javdb_id and existing.metadata_source:
            movie = MovieService.movie_list_query().where(Movie.id == existing.id).get()
            yield "completed", {
                "success": True,
                "movies": [MovieListItemResource.from_attributes_model(movie).model_dump(exclude={"can_play"})],
                "failed_items": [],
                "stats": {"total": 1, "created_count": 0, "already_exists_count": 1, "failed_count": 0},
            }
            return

        provider = build_javdb_provider()
        try:
            with MetadataSourceService.fetch(normalized_movie_number, provider) as (detail, source):
                # 先把搜索命中的原始远端信息回给前端，再开始实际落库。
                yield "movie_found", {
                    "movies": [
                        {
                            "javdb_id": detail.javdb_id if source is None else None,
                            "movie_number": detail.movie_number,
                            "title": detail.title,
                            "cover_image": detail.cover_image if source is None else None,
                        }
                    ],
                    "total": 1,
                }
                yield "upsert_started", {"total": 1}

                created_count = 0
                already_exists_count = 0
                failed_count = 0
                failed_items: list[dict[str, str]] = []
                imported_movies: list[MovieListItemResource] = []
                stats = {
                    "total": 1,
                    "created_count": created_count,
                    "already_exists_count": already_exists_count,
                    "failed_count": failed_count,
                }

                try:
                    # 纯新建语义：已存在影片跳过不更新；重新走列表查询确保响应里带上封面和 can_play 等派生字段。
                    service = cls._build_catalog_import_service()
                    movie, created = (
                        service.import_movie_if_missing(detail)
                        if source is None
                        else service.import_plugin_movie(detail, source, provider)
                    )
                    movie_with_cover = MovieService.movie_list_query().where(Movie.id == movie.id).get_or_none() or movie
                    imported_movies.append(MovieListItemResource.from_attributes_model(movie_with_cover))
                    if created:
                        created_count += 1
                    else:
                        already_exists_count += 1
                except ImageDownloadError as exc:
                    failed_count += 1
                    logger.warning(
                        "Javdb movie image download failed movie_number={} detail={}",
                        normalized_movie_number,
                        exc,
                    )
                    failed_items.append(
                        {
                            "movie_number": normalized_movie_number,
                            "reason": "image_download_failed",
                            "detail": str(exc),
                        }
                    )
                except Exception as exc:
                    failed_count += 1
                    logger.exception(
                        "Javdb movie import failed movie_number={} detail={}",
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

                stats["created_count"] = created_count
                stats["already_exists_count"] = already_exists_count
                stats["failed_count"] = failed_count
                yield "upsert_finished", stats

                if imported_movies:
                    yield "completed", {
                        "success": True,
                        "movies": [movie_item.model_dump(exclude={"can_play"}) for movie_item in imported_movies],
                        "failed_items": failed_items,
                        "stats": stats,
                    }
                    return

                yield "completed", {
                    "success": False,
                    "reason": "internal_error",
                    "movies": [],
                    "failed_items": failed_items,
                    "stats": stats,
                }

        except MetadataNotFoundError:
            yield "completed", {"success": False, "reason": "movie_not_found", "movies": []}
        except Exception as exc:
            logger.exception("Movie metadata search failed movie_number={} detail={}", normalized_movie_number, exc)
            yield "completed", {"success": False, "reason": "internal_error", "movies": []}

    @classmethod
    def stream_import_series_movies_from_javdb(
        cls,
        series_id: int,
    ) -> Iterator[tuple[str, dict]]:
        """按 SSE 事件顺序输出系列影片抓取和导入进度。"""
        yield "search_started", {"series_id": series_id}

        local_series = MovieSeries.get_or_none(MovieSeries.id == series_id)
        if local_series is None:
            yield "completed", {"success": False, "reason": "local_series_not_found", "movies": []}
            return

        series_name = local_series.name.strip()
        yield "series_found", {"series_id": local_series.id, "series_name": series_name}

        provider = build_javdb_provider()
        try:
            series_candidates = provider.search_series(series_name)
        except Exception as exc:
            logger.exception("Javdb series search failed series_id={} series_name={} detail={}", series_id, series_name, exc)
            yield "completed", {"success": False, "reason": "metadata_fetch_failed", "movies": []}
            return

        # 只接受精确同名系列，避免把相似系列误导入本地系列。
        javdb_series = next(
            (candidate for candidate in series_candidates if candidate.name.strip() == series_name),
            None,
        )
        if javdb_series is None:
            yield "completed", {"success": False, "reason": "javdb_series_not_found", "movies": []}
            return

        yield "javdb_series_found", {
            "javdb_id": javdb_series.javdb_id,
            "javdb_type": javdb_series.javdb_type,
            "name": javdb_series.name,
            "videos_count": javdb_series.videos_count,
        }

        try:
            remote_movies = provider.get_series_movies(
                javdb_series.javdb_id,
                series_type=javdb_series.javdb_type,
            )
        except Exception as exc:
            logger.exception(
                "Javdb series movies fetch failed series_id={} javdb_series_id={} detail={}",
                series_id,
                javdb_series.javdb_id,
                exc,
            )
            yield "completed", {"success": False, "reason": "metadata_fetch_failed", "movies": []}
            return

        deduplicated_movies = []
        seen_movie_keys: set[str] = set()
        for movie_item in remote_movies:
            movie_key = movie_item.javdb_id or movie_item.movie_number
            if movie_key in seen_movie_keys:
                continue
            seen_movie_keys.add(movie_key)
            deduplicated_movies.append(movie_item)

        total = len(deduplicated_movies)
        if total == 0:
            yield "completed", {"success": False, "reason": "javdb_series_movies_not_found", "movies": []}
            return

        yield "movie_found", {
            "movies": [
                {
                    "javdb_id": movie_item.javdb_id,
                    "movie_number": movie_item.movie_number,
                    "title": movie_item.title,
                    "cover_image": movie_item.cover_image,
                }
                for movie_item in deduplicated_movies
            ],
            "total": total,
        }
        yield "upsert_started", {"total": total}

        created_count = 0
        already_exists_count = 0
        failed_count = 0
        skipped_items: list[dict[str, str]] = []
        failed_items: list[dict[str, str]] = []
        imported_movies: list[MovieListItemResource] = []
        import_service = cls._build_catalog_import_service()

        for index, movie_item in enumerate(deduplicated_movies, start=1):
            existing_movie = Movie.get_or_none(
                (Movie.javdb_id == movie_item.javdb_id) | (Movie.movie_number == movie_item.movie_number)
            )
            if existing_movie is not None:
                already_exists_count += 1
                skipped_item = {
                    "javdb_id": movie_item.javdb_id,
                    "movie_number": movie_item.movie_number,
                    "reason": "already_exists",
                }
                skipped_items.append(skipped_item)
                yield "movie_skipped", {**skipped_item, "index": index, "total": total}
                continue

            yield "movie_upsert_started", {
                "javdb_id": movie_item.javdb_id,
                "movie_number": movie_item.movie_number,
                "index": index,
                "total": total,
            }
            try:
                # 列表项信息不完整，入库前必须再拉详情复用统一导入链路；外层已跳过已存在影片。
                detail = provider.get_movie_by_javdb_id(movie_item.javdb_id)
                movie, _created = import_service.import_movie_if_missing(detail)
                movie_with_cover = MovieService.movie_list_query().where(Movie.id == movie.id).get_or_none() or movie
                imported_movies.append(MovieListItemResource.from_attributes_model(movie_with_cover))
                created_count += 1
                yield "movie_upsert_finished", {
                    "javdb_id": detail.javdb_id,
                    "movie_number": detail.movie_number,
                    "index": index,
                    "total": total,
                }
            except ImageDownloadError as exc:
                failed_count += 1
                logger.warning(
                    "Javdb series movie image download failed series_id={} javdb_id={} detail={}",
                    series_id,
                    movie_item.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": movie_item.javdb_id,
                        "movie_number": movie_item.movie_number,
                        "reason": "image_download_failed",
                        "detail": str(exc),
                    }
                )
            except MetadataRequestError as exc:
                failed_count += 1
                logger.warning(
                    "Javdb series movie metadata fetch failed series_id={} javdb_id={} detail={}",
                    series_id,
                    movie_item.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": movie_item.javdb_id,
                        "movie_number": movie_item.movie_number,
                        "reason": "metadata_fetch_failed",
                        "detail": str(exc),
                    }
                )
            except Exception as exc:
                failed_count += 1
                logger.exception(
                    "Javdb series movie import failed series_id={} javdb_id={} detail={}",
                    series_id,
                    movie_item.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": movie_item.javdb_id,
                        "movie_number": movie_item.movie_number,
                        "reason": "upsert_failed",
                        "detail": str(exc),
                    }
                )

        stats = {
            "total": total,
            "created_count": created_count,
            "already_exists_count": already_exists_count,
            "failed_count": failed_count,
        }
        yield "upsert_finished", stats

        if imported_movies or skipped_items:
            yield "completed", {
                "success": True,
                "movies": [movie_item.model_dump(exclude={"can_play"}) for movie_item in imported_movies],
                "skipped_items": skipped_items,
                "failed_items": failed_items,
                "stats": stats,
            }
            return

        yield "completed", {
            "success": False,
            "reason": "internal_error",
            "movies": [],
            "skipped_items": skipped_items,
            "failed_items": failed_items,
            "stats": stats,
        }
