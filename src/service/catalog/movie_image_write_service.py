"""影片图片写后联动：显式替换出口（插件调用）与宿主补录共用的派生维护。

封面/剧照的显式替换与宿主自动补图分开：只有拿到图片交付路径的调用方才能替换
既有图片，并在同一个事务里接管 ``field_owners`` 里的图片 owner。落库前后的派生
联动（薄封面重算、旧图清理、资产包重建、图搜向量删除）统一收敛在本模块，
宿主补录与严格刷新复用同一份实现。
"""

from __future__ import annotations

from uuid import uuid4

from loguru import logger
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError

from src.common.media_paths import movie_asset_relative_dir, normalize_asset_dir_name
from src.model import Image, Movie, MoviePlotImage, get_database
from src.plugins.image_delivery import resolve_plugin_image_path
from src.service.catalog.movie_asset_pack_service import MovieAssetPackService
from src.service.catalog.movie_image_service import (
    MovieImageService,
    ThinCoverResolution,
)
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway

# 只放宿主图片入口确实能解码的格式：HEIC/HEIF 依赖 pillow-heif 注册（图片入口未注册），
# 放进来只会让插件交付的 .heic/.heif 在解析阶段报错。
_EXTENSION_FORMATS: dict[str, frozenset[str]] = {
    ".jpg": frozenset({"JPEG", "MPO"}),
    ".jpeg": frozenset({"JPEG", "MPO"}),
    ".png": frozenset({"PNG"}),
    ".webp": frozenset({"WEBP"}),
    ".gif": frozenset({"GIF"}),
    ".avif": frozenset({"AVIF"}),
}


class _ImageWriteConflict(Exception):
    """owner 或 revision 不匹配，事务整体回滚。"""


def _movie_asset_dir(movie_number: str):
    """影片图片目录（相对图片根），封面/剧照/薄封面与资产包同层。"""
    return movie_asset_relative_dir(normalize_asset_dir_name(movie_number))


def delete_plot_image_vectors(plot_image_ids: list[int]) -> None:
    """删除被替换剧照的图搜向量；未启用图搜或删除失败只告警，不影响主流程。"""
    if not plot_image_ids:
        return
    try:
        from src.service.discovery.qdrant_plot_image_store import (
            get_qdrant_plot_image_store,
        )
        from src.service.system.optional_services import image_search_enabled

        if image_search_enabled():
            get_qdrant_plot_image_store().delete_by_plot_image_ids(plot_image_ids)
    except Exception as exc:
        logger.warning(
            "Delete plot image vectors failed count={} detail={}",
            len(plot_image_ids),
            exc,
        )


def apply_thin_cover_resolution(
    image_service: MovieImageService,
    movie: Movie,
    old_thin_cover_image: Image | None,
    resolution: ThinCoverResolution,
    plot_images_by_index: dict[int, Image],
    *,
    refreshed: bool,
) -> set[str]:
    """持久化薄封面解析结果并回写 Movie 引用；返回待清理的旧图路径。

    薄封面 Image 记录的持久化留在图片服务，Movie 的回写留在编排层，
    保证图片服务不写 Movie。
    """
    new_thin_cover_image = image_service.resolve_persisted_thin_cover_image(
        resolution,
        plot_images_by_index,
        refreshed=refreshed,
    )
    movie.thin_cover_image = new_thin_cover_image
    movie.save(only=[Movie.thin_cover_image])
    if old_thin_cover_image is None:
        return set()
    if new_thin_cover_image is not None and old_thin_cover_image.id == new_thin_cover_image.id:
        return set()
    return image_service.delete_image_record_if_unused(old_thin_cover_image)


def refresh_movie_thin_cover(
    movie_id: int,
    image_service: MovieImageService,
    *,
    token: str | None = None,
    rebuild_asset_pack: bool = True,
) -> bool:
    """基于已落盘封面/剧照重算薄封面并清理旧图；返回重算后是否存在薄封面。

    ``token`` 供显式换图路径生成新文件名，避免客户端长缓存吃到旧薄封面；
    调用方随后仍会重建资产包时传 ``rebuild_asset_pack=False``，避免重复打包。
    """
    obsolete_paths: set[str] = set()
    with get_database().atomic():
        movie = Movie.select().where(Movie.id == movie_id).for_update().get()
        old_thin_cover_image = movie.thin_cover_image
        plot_links = list(
            MoviePlotImage.select(MoviePlotImage, Image)
            .join(Image)
            .where(MoviePlotImage.movie == movie)
            .order_by(MoviePlotImage.id)
        )
        resolution = image_service.resolve_thin_cover_from_existing_movie(
            movie, plot_links, token=token
        )
        if (
            resolution.generated_task is None
            and resolution.selected_plot_index is None
            and old_thin_cover_image is None
        ):
            # 没有可用图片来源且当前没有薄封面：无变更，跳过写库与资产包重建。
            return False
        obsolete_paths.update(
            apply_thin_cover_resolution(
                image_service,
                movie,
                old_thin_cover_image,
                resolution,
                {index: link.image for index, link in enumerate(plot_links)},
                refreshed=False,
            )
        )
        movie_number = movie.movie_number
    image_service.delete_obsolete_image_files(obsolete_paths)
    if rebuild_asset_pack:
        MovieAssetPackService.rebuild_movie_asset_pack(_movie_asset_dir(movie_number))
    return Movie.get_by_id(movie_id).thin_cover_image_id is not None


class MovieImageWriteService:
    """插件显式写封面/剧照的唯一入口；图片路径必须来自插件自己的 data/ 目录。"""

    def __init__(self, image_service: MovieImageService | None = None):
        self.image_service = image_service or MovieImageService()

    @staticmethod
    def _validated_source_path(plugin_id: str, image_path: str) -> str:
        path = resolve_plugin_image_path(plugin_id, image_path)
        suffix = path.suffix.lower()
        expected_formats = _EXTENSION_FORMATS.get(suffix)
        if expected_formats is None:
            raise ValueError(
                "图片扩展名必须是 " + " / ".join(sorted(_EXTENSION_FORMATS))
            )
        try:
            with PillowImage.open(path) as image:
                image.load()
                detected = image.format
        except (OSError, UnidentifiedImageError, PillowImage.DecompressionBombError) as exc:
            raise ValueError("图片文件无法解析") from exc
        if detected not in expected_formats:
            raise ValueError("图片扩展名与实际图片格式不一致")
        return str(path)

    def set_cover(
        self,
        movie_id: int,
        plugin_id: str,
        image_path: str,
        expected_revision: int,
    ) -> bool:
        """用插件交付的图片替换封面并取得 owner；冲突或版本不符返回 False。"""
        movie = Movie.get_or_none(Movie.id == movie_id)
        if movie is None:
            return False
        source_path = self._validated_source_path(plugin_id, image_path)
        obsolete_paths: set[str] = set()
        with self.image_service.prepare_metadata_images(
            movie.movie_number,
            source_path,
            [],
            local=True,
            generate_thin_cover=False,
        ) as (cover_task, _plot_tasks, _actor_tasks, _thin):
            if cover_task is None:
                return False
            try:
                with get_database().atomic():
                    locked = (
                        Movie.select().where(Movie.id == movie_id).for_update().get()
                    )
                    old_cover = locked.cover_image
                    new_cover = self.image_service.persist_prepared_image(cover_task)
                    if new_cover is None or not MovieOwnershipGateway.replace_cover_image(
                        movie_id, plugin_id, new_cover.id, expected_revision
                    ):
                        raise _ImageWriteConflict()
            except _ImageWriteConflict:
                return False
            if old_cover is not None and old_cover.id != new_cover.id:
                obsolete_paths = self.image_service.delete_image_record_if_unused(
                    old_cover
                )
        self.image_service.delete_obsolete_image_files(obsolete_paths)
        # 显式换图后重算薄封面，并带 token 生成新文件名，避免客户端长缓存吃到旧薄封面。
        refresh_movie_thin_cover(
            movie_id, self.image_service, token=uuid4().hex, rebuild_asset_pack=False
        )
        MovieAssetPackService.rebuild_movie_asset_pack(
            _movie_asset_dir(movie.movie_number)
        )
        return True

    def set_plot_images(
        self,
        movie_id: int,
        plugin_id: str,
        image_paths: list[str],
        expected_revision: int,
    ) -> bool:
        """整组替换剧照并取得 owner；传空列表表示清空剧照。"""
        if isinstance(image_paths, (str, bytes)) or image_paths is None:
            raise ValueError("image_paths 必须是图片路径序列")
        movie = Movie.get_or_none(Movie.id == movie_id)
        if movie is None:
            return False
        source_paths = [
            self._validated_source_path(plugin_id, path) for path in image_paths
        ]
        if not source_paths:
            return self._replace_plot_images(
                movie, plugin_id, [], expected_revision
            )
        with self.image_service.prepare_metadata_images(
            movie.movie_number,
            None,
            source_paths,
            local=True,
            generate_thin_cover=False,
        ) as (_cover_task, plot_tasks, _actor_tasks, _thin):
            return self._replace_plot_images(
                movie, plugin_id, plot_tasks, expected_revision
            )

    def _replace_plot_images(
        self,
        movie: Movie,
        plugin_id: str,
        plot_tasks,
        expected_revision: int,
    ) -> bool:
        obsolete_images: list[Image] = []
        old_plot_image_ids: list[int] = []
        try:
            with get_database().atomic():
                locked_movie = (
                    Movie.select().where(Movie.id == movie.id).for_update().get()
                )
                if not MovieOwnershipGateway.take_plot_images_ownership(
                    movie.id, plugin_id, expected_revision
                ):
                    raise _ImageWriteConflict()
                old_links = list(
                    MoviePlotImage.select().where(MoviePlotImage.movie == movie.id)
                )
                old_plot_image_ids = [link.id for link in old_links]
                obsolete_images = [link.image for link in old_links]
                MoviePlotImage.delete().where(
                    MoviePlotImage.movie == movie.id
                ).execute()
                persisted = self.image_service.persist_prepared_images(plot_tasks)
                for task in plot_tasks:
                    image = persisted.get(task.relative_path)
                    if image is None:
                        continue
                    MoviePlotImage.create(movie=locked_movie, image=image)
        except _ImageWriteConflict:
            return False
        refresh_movie_thin_cover(
            movie.id, self.image_service, token=uuid4().hex, rebuild_asset_pack=False
        )
        obsolete_paths: set[str] = set()
        for image in obsolete_images:
            obsolete_paths.update(self.image_service.delete_image_record_if_unused(image))
        self.image_service.delete_obsolete_image_files(obsolete_paths)
        delete_plot_image_vectors(old_plot_image_ids)
        MovieAssetPackService.rebuild_movie_asset_pack(
            _movie_asset_dir(movie.movie_number)
        )
        return True
