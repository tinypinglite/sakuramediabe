"""把元数据插件交付的完整影片资料导入本地目录。"""

from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

from loguru import logger
from peewee import IntegrityError

from src.common.media_paths import movie_asset_relative_dir, normalize_asset_dir_name
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import find_movie_by_number
from src.model import (
    Image,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieTag,
    Tag,
    get_database,
)
from src.plugins.extensions.metadata import PluginMovieMetadata
from src.service.catalog.actor_resolution_service import (
    ActorLookupIndex,
    ActorResolutionService,
)
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService
from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    ImagePersistTask,
    MovieImageService,
    PreparedImageFile,
    ThinCoverResolution,
)
from src.service.catalog.movie_image_write_service import (
    apply_thin_cover_resolution,
    refresh_movie_thin_cover,
)

__all__ = [
    "CatalogImportService",
    "ImageDownloadError",
    "ImagePersistTask",
    "PreparedImageFile",
    "ThinCoverResolution",
]


class CatalogImportService:
    def __init__(
        self,
        image_downloader: Callable[[str, Path], None] | None = None,
        persist_lock=None,
    ):
        self.image_service = MovieImageService(image_downloader=image_downloader)
        self.persist_lock = persist_lock
        self.actor_lookup = ActorLookupIndex()

    def reset_actor_lookup(self) -> None:
        """每批导入开始时丢弃旧快照；首次演员查找会重新加载。"""
        self.actor_lookup.invalidate()

    @staticmethod
    def _resolve_movie_series(series_name: str | None) -> MovieSeries | None:
        return Movie.resolve_series(series_name)

    @staticmethod
    def _movie_asset_dir(movie: Movie):
        return movie_asset_relative_dir(normalize_asset_dir_name(movie.movie_number))

    def _apply_thin_cover_resolution(
        self,
        movie: Movie,
        old_thin_cover_image: Image | None,
        resolution: ThinCoverResolution,
        plot_images_by_index: dict[int, Image],
        *,
        refreshed: bool,
    ) -> set[str]:
        return apply_thin_cover_resolution(
            self.image_service,
            movie,
            old_thin_cover_image,
            resolution,
            plot_images_by_index,
            refreshed=refreshed,
        )

    def import_movie(
        self,
        detail: PluginMovieMetadata,
        source: dict[str, str | None],
        *,
        force_subscribed: bool = False,
    ) -> tuple[Movie, bool]:
        """按番号新建影片；已存在则原样返回，不更新任何字段。"""
        existing = find_movie_by_number(detail.movie_number)
        if existing is not None:
            return existing, False

        actors = ActorResolutionService.resolve_movie_actors(
            detail.actors,
            image_service=self.image_service,
            lookup=self.actor_lookup,
        )
        with self.image_service.prepare_metadata_images(
            detail.movie_number,
            detail.cover_image_path,
            detail.plot_image_paths,
            local=True,
        ) as (cover, plots, _actor_tasks, thin):
            try:
                with self.persist_lock or nullcontext(), get_database().atomic():
                    existing = find_movie_by_number(detail.movie_number)
                    if existing is not None:
                        return existing, False
                    movie = Movie.create(
                        movie_number=detail.movie_number,
                        title=detail.title,
                        summary=detail.summary,
                        release_date=detail.release_date,
                        duration_minutes=detail.duration_minutes,
                        maker_name=detail.maker_name,
                        director_name=detail.director_name,
                        series=self._resolve_movie_series(detail.series_name),
                        cover_image=self.image_service.persist_prepared_image(cover),
                        metadata_source=source,
                        is_subscribed=force_subscribed,
                        subscribed_at=utc_now_for_db() if force_subscribed else None,
                    )
                    for actor in actors:
                        MovieActor.get_or_create(movie=movie, actor=actor)
                    for name in detail.tags:
                        tag, _ = Tag.get_or_create(name=name)
                        MovieTag.get_or_create(movie=movie, tag=tag)
                    images = self.image_service.persist_prepared_images(plots)
                    by_index = {}
                    for task in plots:
                        image = images[task.relative_path]
                        MoviePlotImage.create(movie=movie, image=image)
                        by_index[task.plot_index] = image
                    self._apply_thin_cover_resolution(
                        movie, None, thin, by_index, refreshed=False
                    )
            except IntegrityError:
                existing = find_movie_by_number(detail.movie_number)
                if existing is None:
                    raise
                return existing, False
        MovieAssetPackService.rebuild_movie_asset_pack(self._movie_asset_dir(movie))
        logger.info(
            "Catalog import finished movie_id={} movie_number={} source={}",
            movie.id,
            movie.movie_number,
            source.get("plugin_id"),
        )
        return movie, True

    def backfill_movie_thin_cover(self, movie: Movie) -> bool:
        return refresh_movie_thin_cover(movie.id, self.image_service)
