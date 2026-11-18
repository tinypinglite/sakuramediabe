"""按当前元数据源顺序刷新已存在影片的资料与自动管理图片。"""

from __future__ import annotations

from contextlib import ExitStack
from uuid import uuid4

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_paths import movie_asset_relative_dir, normalize_asset_dir_name
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Image,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieTag,
    Tag,
    get_database,
)
from src.model.catalog.movies import MOVIE_ACTOR_LIST_FIELD
from src.plugins.extensions.metadata import PluginMovieMetadata
from src.service.catalog.actor_resolution_service import ActorResolutionService
from src.service.catalog.metadata_source_service import (
    MetadataSourceError,
    MetadataSourceService,
)
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService
from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    MovieImageService,
)
from src.service.catalog.movie_image_write_service import (
    delete_plot_image_vectors,
    refresh_movie_thin_cover,
)
from src.service.catalog.movie_ownership_gateway import (
    MANUAL_MOVIE_FIELD_OWNER,
    MovieOwnershipGateway,
)
from src.service.catalog.movie_service import MovieService


class MovieMetadataRefreshService:
    """刷新现有影片；人工/插件接管的字段和图片槽位保持原值。"""

    @staticmethod
    def _movie_asset_dir(movie_number: str):
        return movie_asset_relative_dir(normalize_asset_dir_name(movie_number))

    @classmethod
    def refresh_movie_metadata(cls, movie_number: str):
        movie, normalized_movie_number = MovieService.require_movie_by_normalized_number(
            movie_number
        )
        if not MetadataSourceService.enabled_plugin_sources():
            raise ApiError(
                503,
                "metadata_source_unavailable",
                "未启用任何元数据插件",
                {"movie_number": movie.movie_number},
            )

        try:
            with MetadataSourceService.fetch(normalized_movie_number) as outcome:
                refreshed = cls._refresh_records(
                    movie,
                    outcome.detail,
                    outcome.source,
                )
        except MetadataNotFoundError as exc:
            raise ApiError(
                404,
                "movie_metadata_not_found",
                "影片远端元数据不存在",
                {
                    "movie_number": movie.movie_number,
                    "normalized_movie_number": normalized_movie_number,
                },
            ) from exc
        except MetadataSourceError as exc:
            raise ApiError(
                502,
                "movie_metadata_refresh_failed",
                "影片元数据刷新失败",
                {
                    "movie_number": movie.movie_number,
                    "normalized_movie_number": normalized_movie_number,
                    "source_errors": [item.as_dict() for item in exc.failures],
                },
            ) from exc
        except ImageDownloadError as exc:
            logger.warning(
                "Movie metadata refresh image failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            raise ApiError(
                502,
                "movie_metadata_refresh_failed",
                "影片元数据刷新失败",
                {
                    "movie_number": movie.movie_number,
                    "normalized_movie_number": normalized_movie_number,
                    "detail": str(exc),
                },
            ) from exc
        except ApiError:
            raise
        except Exception as exc:
            logger.exception(
                "Movie metadata refresh failed movie_number={} detail={}",
                normalized_movie_number,
                exc,
            )
            raise ApiError(
                502,
                "movie_metadata_refresh_failed",
                "影片元数据刷新失败",
                {
                    "movie_number": movie.movie_number,
                    "normalized_movie_number": normalized_movie_number,
                    "detail": str(exc),
                },
            ) from exc

        return MovieService.get_movie_detail(refreshed.movie_number)

    @classmethod
    def _refresh_records(
        cls,
        movie: Movie,
        detail: PluginMovieMetadata,
        source: dict[str, str | None],
    ) -> Movie:
        movie_number = movie.movie_number
        owners = movie.field_owners or {}
        cover_managed = owners.get("cover_image") is None
        plots_managed = owners.get("plot_images") is None
        actors_managed = (
            owners.get(MOVIE_ACTOR_LIST_FIELD) != MANUAL_MOVIE_FIELD_OWNER
        )

        image_service = MovieImageService()
        cover_path = detail.cover_image_path if cover_managed else None
        plot_paths = (
            list(detail.plot_image_paths)
            if plots_managed and detail.plot_image_paths
            else []
        )

        with ExitStack() as stack:
            cover_task, plot_tasks, _actor_tasks, _thin = stack.enter_context(
                image_service.prepare_metadata_images(
                    movie_number,
                    cover_path,
                    plot_paths,
                    local=True,
                    generate_thin_cover=False,
                )
            )
            actors = (
                ActorResolutionService.resolve_movie_actors(
                    detail.actors,
                    image_service=image_service,
                )
                if actors_managed and detail.actors
                else []
            )
            stack.enter_context(get_database().atomic())

            locked = Movie.select().where(Movie.id == movie.id).for_update().get()
            locked_owners = locked.field_owners or {}
            old_cover = locked.cover_image
            old_plot_links = list(
                MoviePlotImage.select(MoviePlotImage, Image)
                .join(Image)
                .where(MoviePlotImage.movie == locked.id)
                .order_by(MoviePlotImage.id)
            )
            obsolete_images = [link.image for link in old_plot_links]
            old_plot_image_ids = [link.id for link in old_plot_links]

            locked.release_date = detail.release_date
            locked.duration_minutes = detail.duration_minutes
            locked.metadata_source = source
            if detail.series_name:
                locked.series = Movie.resolve_series(detail.series_name)
            locked.save(
                only=[
                    Movie.release_date,
                    Movie.duration_minutes,
                    Movie.metadata_source,
                    *([Movie.series] if detail.series_name else []),
                ]
            )

            protected_fields = {"title": detail.title}
            if detail.summary.strip():
                protected_fields["summary"] = detail.summary
            if detail.maker_name:
                protected_fields["maker_name"] = detail.maker_name
            if detail.director_name:
                protected_fields["director_name"] = detail.director_name
            MovieOwnershipGateway.update_host_unowned(locked.id, protected_fields)

            replace_actors = (
                bool(actors)
                and locked_owners.get(MOVIE_ACTOR_LIST_FIELD)
                != MANUAL_MOVIE_FIELD_OWNER
            )
            if replace_actors:
                MovieActor.delete().where(MovieActor.movie == locked.id).execute()
                for actor in actors:
                    MovieActor.get_or_create(movie=locked, actor=actor)

            if detail.tags:
                MovieTag.delete().where(MovieTag.movie == locked.id).execute()
                for name in dict.fromkeys(
                    tag.strip() for tag in detail.tags if tag.strip()
                ):
                    tag, _ = Tag.get_or_create(name=name)
                    MovieTag.get_or_create(movie=locked, tag=tag)

            images_changed = False
            if cover_task is not None and locked_owners.get("cover_image") is None:
                new_cover = image_service.persist_prepared_image(cover_task)
                if new_cover is not None:
                    locked.cover_image = new_cover
                    locked.save(only=[Movie.cover_image])
                    images_changed = True
                    if old_cover is not None:
                        obsolete_images.append(old_cover)

            if plot_tasks and locked_owners.get("plot_images") is None:
                persisted = image_service.persist_prepared_images(plot_tasks)
                MoviePlotImage.delete().where(
                    MoviePlotImage.movie == locked.id
                ).execute()
                for task in plot_tasks:
                    image = persisted.get(task.relative_path)
                    if image is None:
                        continue
                    MoviePlotImage.create(movie=locked, image=image)
                images_changed = True
            else:
                old_plot_image_ids = []

            movie_id = locked.id

        obsolete_paths: set[str] = set()
        for image in obsolete_images:
            obsolete_paths.update(image_service.delete_image_record_if_unused(image))
        if images_changed:
            refresh_movie_thin_cover(
                movie_id,
                image_service,
                token=uuid4().hex,
                rebuild_asset_pack=False,
            )
        delete_plot_image_vectors(old_plot_image_ids)
        image_service.delete_obsolete_image_files(obsolete_paths)
        MovieAssetPackService.rebuild_movie_asset_pack(
            cls._movie_asset_dir(movie_number)
        )
        logger.info(
            "Movie metadata refresh finished movie_id={} movie_number={} source={}",
            movie_id,
            movie_number,
            source.get("plugin_id"),
        )
        return Movie.get_by_id(movie_id)
