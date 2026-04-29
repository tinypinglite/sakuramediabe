"""目录导入 service。

负责把 JavDB 返回的影片/演员详情转换成本地目录数据，并处理图片下载与图片记录持久化。
阅读入口建议从 ``upsert_movie_from_javdb_detail`` 开始，再看图片任务的构建、下载和入库 helper。
"""

from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from loguru import logger
from PIL import Image as PillowImage, UnidentifiedImageError
from sakuramedia_metadata_providers.providers.dmm import DmmProvider

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import Actor, Image, MediaThumbnail, Movie, MovieActor, MoviePlotImage, MovieSeries, MovieTag, Tag, get_database
from sakuramedia_metadata_providers.models import JavdbMovieActorResource, JavdbMovieDetailResource
from src.service.catalog.image_cleanup_service import ImageCleanupService
from src.service.catalog.movie_collection_service import MovieCollectionService
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.system.resource_task_state_service import ResourceTaskStateService


class ImageDownloadError(Exception):
    pass


@dataclass
class ImagePersistTask:
    image_type: str
    image_url: str
    relative_path: str
    absolute_path: Path
    plot_index: Optional[int] = None


@dataclass
class PreparedImageFile:
    image_task: ImagePersistTask
    temp_path: Path
    temp_root: Path


@dataclass
class ThinCoverResolution:
    generated_task: ImagePersistTask | None = None
    generated_prepared_file: PreparedImageFile | None = None
    selected_plot_index: int | None = None


class CatalogImportService:
    """承接远端元数据到本地目录模型的 upsert。"""
    TASK_KEY = "movie_desc_sync"

    # 图片下载重试次数
    IMAGE_DOWNLOAD_MAX_RETRIES = 6
    # 图片下载超时秒数
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        image_downloader: Callable[[str, Path], None] | None = None,
        persist_lock=None,
        dmm_provider: DmmProvider | None = None,
    ):
        self.http_client = httpx.Client(
            timeout=self.IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
            trust_env=False,
        )
        self.image_downloader = image_downloader or self._download_image
        self.persist_lock = persist_lock
        self.dmm_provider = dmm_provider or self._build_dmm_provider()

    @staticmethod
    def _build_dmm_provider() -> DmmProvider:
        from src.metadata.factory import build_dmm_provider
        return build_dmm_provider()

    @staticmethod
    def _split_actor_alias_name(alias_name: str) -> List[str]:
        return [name.strip() for name in (alias_name or "").split("/") if name.strip()]

    @classmethod
    def _merge_actor_alias_name(
        cls,
        primary_name: str,
        alias_names: List[str],
        existing_alias_name: str,
    ) -> str:
        merged_aliases: List[str] = []
        seen_aliases: set[str] = set()

        # 搜索来源别名优先，保证后续本地搜索尽量贴近 JavDB 返回结果。
        for candidate_name in [primary_name, *alias_names, *cls._split_actor_alias_name(existing_alias_name)]:
            normalized_name = (candidate_name or "").strip()
            if not normalized_name:
                continue
            dedupe_key = normalized_name.casefold()
            if dedupe_key in seen_aliases:
                continue
            seen_aliases.add(dedupe_key)
            merged_aliases.append(normalized_name)

        return " / ".join(merged_aliases)

    @staticmethod
    def _resolve_movie_series(series_name: str | None) -> MovieSeries | None:
        return Movie.resolve_series(series_name)

    @staticmethod
    def _normalize_owner_key(owner_key: str) -> str:
        return re.sub(r"[^0-9A-Za-z._-]", "_", owner_key).strip("._-") or "unknown"

    @staticmethod
    def _normalize_image_extension(raw_extension: str | None) -> str:
        normalized_extension = (raw_extension or "").strip().lower()
        if not normalized_extension:
            return ".jpg"
        if not normalized_extension.startswith("."):
            normalized_extension = f".{normalized_extension}"
        if len(normalized_extension) > 8:
            return ".jpg"
        return normalized_extension

    @classmethod
    def _detect_split_points(cls, image, center_range: int = 100) -> tuple[int, int]:
        import cv2
        import numpy as np

        height, width = image.shape[:2]
        center_x = width // 2
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        sobel = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        gradient_magnitude = np.abs(sobel)
        column_gradient = np.sum(gradient_magnitude, axis=0)
        max_gradient = float(column_gradient.max()) if column_gradient.size else 0.0
        if max_gradient <= 0:
            return -1, -1
        column_gradient /= max_gradient
        left_range = range(max(0, center_x - center_range), center_x)
        right_range = range(center_x, min(width, center_x + center_range))
        try:
            left_point = max(left_range, key=lambda index: column_gradient[index])
            right_point = max(right_range, key=lambda index: column_gradient[index])
        except ValueError:
            return -1, -1
        left_distance = abs(center_x - left_point)
        right_distance = abs(right_point - center_x)
        # 仅接受左右分割点近似对称，或右侧分割点接近既有经验值的位置。
        if abs(left_distance - right_distance) < 10 or abs(right_distance - 20) < 10:
            return left_point, right_point
        crop_aspect_ratio = (width - right_point) / height if height > 0 else 0
        right_edge_strength = float(column_gradient[right_point])
        # 旧规则失败后，仅对裁出区域仍像竖封面的强边缘切点做保守增强，避免普通横图被固定比例误切。
        wide_spine_matched = (
            12 <= right_distance <= center_range
            and 0.45 <= crop_aspect_ratio <= 0.85
            and right_edge_strength >= 0.35
        )
        narrow_spine_matched = (
            4 <= right_distance <= 11
            and 0.55 <= crop_aspect_ratio <= 0.80
            and right_edge_strength >= 0.50
        )
        center_split_matched = (
            right_distance == 0
            and 0.55 <= crop_aspect_ratio <= 0.80
            and right_edge_strength >= 0.50
        )
        if not (wide_spine_matched or narrow_spine_matched or center_split_matched):
            return -1, -1
        return left_point, right_point

    @classmethod
    def _split_image(cls, image_path: Path, output_image_path: Path, center_range: int = 100) -> bool:
        try:
            import cv2
        except ImportError:
            logger.warning("Thin cover split skipped because cv2 is unavailable source={}", str(image_path))
            return False

        image = cv2.imread(str(image_path))
        if image is None:
            logger.warning("Thin cover split skipped because cover image cannot be read source={}", str(image_path))
            return False
        try:
            left_point, right_point = cls._detect_split_points(image, center_range=center_range)
        except Exception as exc:
            logger.warning("Thin cover split point detection failed source={} detail={}", str(image_path), exc)
            return False
        if left_point == -1 and right_point == -1:
            logger.info("Thin cover split points not found source={}", str(image_path))
            return False
        output_image_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_image_path), image[:, right_point:])
        return True

    @staticmethod
    def _is_portrait_image(image_path: Path) -> bool:
        try:
            with PillowImage.open(image_path) as image:
                width, height = image.size
                return width > 0 and height > width
        except (FileNotFoundError, UnidentifiedImageError, OSError) as exc:
            logger.warning("Thin cover portrait check failed image_path={} detail={}", str(image_path), exc)
            return False

    def _build_generated_thin_cover_task(self, movie_number: str, extension: str) -> ImagePersistTask:
        safe_owner_key = self._normalize_owner_key(movie_number)
        normalized_extension = self._normalize_image_extension(extension)
        relative_path = (Path("movies") / safe_owner_key / f"thin-cover{normalized_extension}").as_posix()
        absolute_path = self._image_root_path() / relative_path
        return ImagePersistTask(
            image_type="thin_cover",
            image_url="",
            relative_path=relative_path,
            absolute_path=absolute_path,
        )

    def _generate_thin_cover_task_from_cover(
        self,
        movie_number: str,
        cover_path: Path,
        extension: str,
    ) -> ImagePersistTask | None:
        thin_cover_task = self._build_generated_thin_cover_task(movie_number, extension)
        if self._split_image(cover_path, thin_cover_task.absolute_path):
            return thin_cover_task
        try:
            thin_cover_task.absolute_path.unlink()
        except FileNotFoundError:
            pass
        return None

    def _generate_prepared_thin_cover_from_cover(
        self,
        movie_number: str,
        cover_path: Path,
        extension: str,
        temp_root: Path,
    ) -> tuple[ImagePersistTask, PreparedImageFile] | None:
        thin_cover_task = self._build_generated_thin_cover_task(movie_number, extension)
        prepared_file = PreparedImageFile(
            image_task=thin_cover_task,
            temp_path=temp_root / thin_cover_task.relative_path,
            temp_root=temp_root,
        )
        if self._split_image(cover_path, prepared_file.temp_path):
            return thin_cover_task, prepared_file
        try:
            prepared_file.temp_path.unlink()
        except FileNotFoundError:
            pass
        return None

    def _select_portrait_plot_index(self, plot_items: List[tuple[int, Path]]) -> int | None:
        # 业务约定只允许前两张剧情图参与竖封面回退，后续剧情图不再参与判定。
        for plot_index, plot_path in plot_items[:2]:
            if self._is_portrait_image(plot_path):
                return plot_index
        return None

    def _resolve_thin_cover_from_downloaded_images(
        self,
        movie_number: str,
        cover_task: ImagePersistTask | None,
        plot_tasks: List[ImagePersistTask],
    ) -> ThinCoverResolution:
        if cover_task is not None and cover_task.absolute_path.exists():
            thin_cover_task = self._generate_thin_cover_task_from_cover(
                movie_number,
                cover_task.absolute_path,
                Path(cover_task.relative_path).suffix,
            )
            if thin_cover_task is not None:
                return ThinCoverResolution(generated_task=thin_cover_task)
        selected_plot_index = self._select_portrait_plot_index(
            [
                (int(plot_task.plot_index), plot_task.absolute_path)
                for plot_task in plot_tasks
                if plot_task.plot_index is not None and plot_task.absolute_path.exists()
            ]
        )
        return ThinCoverResolution(selected_plot_index=selected_plot_index)

    def _resolve_thin_cover_from_prepared_images(
        self,
        movie_number: str,
        cover_task: ImagePersistTask | None,
        plot_tasks: List[ImagePersistTask],
        prepared_files: List[PreparedImageFile],
    ) -> ThinCoverResolution:
        prepared_by_relative_path = {prepared_file.image_task.relative_path: prepared_file for prepared_file in prepared_files}
        if cover_task is not None:
            prepared_cover = prepared_by_relative_path.get(cover_task.relative_path)
            if prepared_cover is not None:
                generated = self._generate_prepared_thin_cover_from_cover(
                    movie_number,
                    prepared_cover.temp_path,
                    Path(cover_task.relative_path).suffix,
                    prepared_cover.temp_root,
                )
                if generated is not None:
                    thin_cover_task, thin_cover_prepared_file = generated
                    return ThinCoverResolution(
                        generated_task=thin_cover_task,
                        generated_prepared_file=thin_cover_prepared_file,
                    )
        selected_plot_index = self._select_portrait_plot_index(
            [
                (int(plot_task.plot_index), prepared_by_relative_path[plot_task.relative_path].temp_path)
                for plot_task in plot_tasks
                if plot_task.plot_index is not None and plot_task.relative_path in prepared_by_relative_path
            ]
        )
        return ThinCoverResolution(selected_plot_index=selected_plot_index)

    def _resolve_thin_cover_from_existing_movie(
        self,
        movie: Movie,
        plot_links: List[MoviePlotImage],
    ) -> ThinCoverResolution:
        cover_image = movie.cover_image
        if cover_image is not None:
            cover_path = self._image_root_path() / cover_image.origin
            thin_cover_task = self._generate_thin_cover_task_from_cover(
                movie.movie_number,
                cover_path,
                Path(cover_image.origin).suffix,
            )
            if thin_cover_task is not None:
                return ThinCoverResolution(generated_task=thin_cover_task)
        selected_plot_index = self._select_portrait_plot_index(
            [
                (plot_index, self._image_root_path() / plot_link.image.origin)
                for plot_index, plot_link in enumerate(plot_links)
            ]
        )
        return ThinCoverResolution(selected_plot_index=selected_plot_index)

    def _apply_thin_cover_resolution(
        self,
        movie: Movie,
        old_thin_cover_image: Image | None,
        resolution: ThinCoverResolution,
        plot_images_by_index: Dict[int, Image],
        *,
        refreshed: bool,
    ) -> set[str]:
        if resolution.generated_task is not None:
            new_thin_cover_image = (
                self._persist_refreshed_image_record(resolution.generated_task)
                if refreshed
                else self._persist_prepared_image(resolution.generated_task)
            )
        elif resolution.selected_plot_index is not None:
            new_thin_cover_image = plot_images_by_index.get(resolution.selected_plot_index)
        else:
            new_thin_cover_image = None
        movie.thin_cover_image = new_thin_cover_image
        movie.save(only=[Movie.thin_cover_image])
        if old_thin_cover_image is None:
            return set()
        if new_thin_cover_image is not None and old_thin_cover_image.id == new_thin_cover_image.id:
            return set()
        return self._delete_image_record_if_unused(old_thin_cover_image)

    def upsert_movie_from_javdb_detail(
        self,
        detail: JavdbMovieDetailResource,
        force_subscribed: bool = False,
    ) -> Movie:
        """把一份 JavDB 影片详情完整落到本地 Movie/Actor/Tag/Image 关系中。"""
        actors = detail.actors or []
        tags = detail.tags or []
        plot_images = detail.plot_images or []
        logger.info(
            "Catalog upsert start movie_number={} javdb_id={} actors={} tags={} plot_images={}",
            detail.movie_number,
            detail.javdb_id,
            len(actors),
            len(tags),
            len(plot_images),
        )
        plot_urls = self._unique_preserve_order(plot_images)
        if len(plot_urls) != len(plot_images):
            logger.debug(
                "Catalog upsert deduplicated plot images movie_number={} original={} deduplicated={}",
                detail.movie_number,
                len(plot_images),
                len(plot_urls),
            )

        # 影片保存进事务前先把全部图片准备好并下载完成，避免事务里做慢速网络 IO。
        cover_task, plot_tasks, actor_image_tasks_by_javdb_id = self._build_movie_import_image_tasks(
            detail.movie_number,
            detail.cover_image,
            plot_urls,
            actors,
        )
        self._download_image_tasks(
            self._collect_image_tasks(
                cover_task,
                plot_tasks,
                actor_image_tasks_by_javdb_id,
            )
        )
        thin_cover_resolution = self._resolve_thin_cover_from_downloaded_images(
            detail.movie_number,
            cover_task,
            plot_tasks,
        )

        lock_context = self.persist_lock or nullcontext()
        obsolete_paths: set[str] = set()
        with lock_context:
            with get_database().atomic():
                # movie_number 和 javdb_id 任一命中都视为同一影片，保证重复导入时走更新。
                movie = Movie.get_or_none((Movie.movie_number == detail.movie_number) | (Movie.javdb_id == detail.javdb_id))
                created_movie = movie is None
                if movie is None:
                    movie = Movie(
                        movie_number=detail.movie_number,
                        javdb_id=detail.javdb_id,
                        title=detail.title,
                    )
                old_thin_cover_image = movie.thin_cover_image
                was_subscribed = bool(movie.is_subscribed)
                target_is_subscribed = True if force_subscribed else detail.is_subscribed

                if cover_task is not None:
                    movie.cover_image = self._persist_prepared_image(cover_task)
                movie.release_date = detail.release_date
                movie.duration_minutes = detail.duration_minutes or 0
                movie.score = detail.score or 0
                movie.score_number = detail.score_number
                movie.watched_count = detail.watched_count
                movie.want_watch_count = detail.want_watch_count
                movie.comment_count = detail.comment_count
                movie.summary = detail.summary
                movie.series = self._resolve_movie_series(detail.series_name)
                # 同步写入影片详情中的厂商和导演名称，保障检索与详情展示一致。
                movie.maker_name = detail.maker_name
                movie.director_name = detail.director_name
                if target_is_subscribed is not None:
                    movie.is_subscribed = target_is_subscribed
                    if target_is_subscribed:
                        if not was_subscribed or movie.subscribed_at is None:
                            movie.subscribed_at = utc_now_for_db()
                    else:
                        movie.subscribed_at = None
                movie.extra = detail.extra
                movie.title = detail.title
                movie.javdb_id = detail.javdb_id
                movie.movie_number = detail.movie_number
                # 手动覆盖优先：已手工标记的影片在导入刷新时保持现状，不按自动规则重算合集状态。
                if not bool(movie.is_collection_overridden):
                    movie.is_collection = MovieCollectionService.matches_configured_collection(
                        detail.movie_number,
                        detail.duration_minutes,
                    )
                movie.save()
                logger.debug(
                    "Catalog upsert movie saved movie_id={} movie_number={} created={}",
                    movie.id,
                    movie.movie_number,
                    created_movie,
                )

                # 演员、标签、剧照关系都使用 get_or_create，避免多次导入产生重复关联。
                for actor_resource in actors:
                    actor = self.upsert_actor_from_javdb_resource(
                        actor_resource,
                        profile_image_task=actor_image_tasks_by_javdb_id.get(actor_resource.javdb_id),
                    )
                    MovieActor.get_or_create(movie=movie, actor=actor)
                    logger.debug(
                        "Catalog upsert actor linked movie_id={} actor_id={} actor_javdb_id={}",
                        movie.id,
                        actor.id,
                        actor.javdb_id,
                    )

                for tag_resource in tags:
                    tag, _ = Tag.get_or_create(name=tag_resource.name)
                    MovieTag.get_or_create(movie=movie, tag=tag)
                    logger.debug("Catalog upsert tag linked movie_id={} tag_id={} tag_name={}", movie.id, tag.id, tag.name)

                plot_images_by_index: Dict[int, Image] = {}
                for plot_task in plot_tasks:
                    plot_image = self._persist_prepared_image(plot_task)
                    if plot_image is not None:
                        if plot_task.plot_index is not None:
                            plot_images_by_index[int(plot_task.plot_index)] = plot_image
                        MoviePlotImage.get_or_create(movie=movie, image=plot_image)
                        logger.debug(
                            "Catalog upsert plot image linked movie_id={} image_id={} index={}",
                            movie.id,
                            plot_image.id,
                            plot_task.plot_index,
                        )
                obsolete_paths.update(
                    self._apply_thin_cover_resolution(
                        movie,
                        old_thin_cover_image,
                        thin_cover_resolution,
                        plot_images_by_index,
                        refreshed=False,
                    )
                )

        self._delete_obsolete_image_files(obsolete_paths)
        MovieHeatService.update_single_movie_heat(movie.id)
        # 主入库先完成，再补 DMM 描述，避免第三方页面波动影响影片基础数据入库。
        self.sync_movie_desc(movie)
        logger.info("Catalog upsert finished movie_id={} movie_number={}", movie.id, movie.movie_number)
        return movie

    def refresh_movie_metadata_strict(
        self,
        movie: Movie,
        detail: JavdbMovieDetailResource,
    ) -> Movie:
        """按远端详情严格刷新影片元数据，不触碰描述、订阅与番号字段。"""
        actors = detail.actors or []
        tags = detail.tags or []
        plot_images = detail.plot_images or []
        plot_urls = self._unique_preserve_order(plot_images)
        cover_task, plot_tasks, actor_image_tasks_by_javdb_id = self._build_movie_import_image_tasks(
            movie.movie_number,
            detail.cover_image,
            plot_urls,
            actors,
        )
        image_tasks = self._collect_image_tasks(
            cover_task,
            plot_tasks,
            actor_image_tasks_by_javdb_id,
        )

        # 严格刷新先把新图片全部下载到临时目录，避免中途失败污染正式目录。
        prepared_files = self._download_image_tasks_to_temporary_files(image_tasks)
        thin_cover_resolution = self._resolve_thin_cover_from_prepared_images(
            movie.movie_number,
            cover_task,
            plot_tasks,
            prepared_files,
        )
        if thin_cover_resolution.generated_prepared_file is not None:
            prepared_files.append(thin_cover_resolution.generated_prepared_file)
        new_relative_paths = {prepared.image_task.relative_path for prepared in prepared_files}
        finalized = False
        obsolete_paths: set[str] = set()
        try:
            lock_context = self.persist_lock or nullcontext()
            with lock_context:
                with get_database().atomic():
                    persisted_movie, obsolete_paths = self._refresh_movie_metadata_records_strict(
                        movie=movie,
                        detail=detail,
                        actors=actors,
                        tags=tags,
                        thin_cover_resolution=thin_cover_resolution,
                        cover_task=cover_task,
                        plot_tasks=plot_tasks,
                        actor_image_tasks_by_javdb_id=actor_image_tasks_by_javdb_id,
                    )
            self._finalize_prepared_image_files(prepared_files)
            self._delete_obsolete_image_files(obsolete_paths - new_relative_paths)
            finalized = True
            logger.info(
                "Catalog strict metadata refresh finished movie_id={} movie_number={}",
                persisted_movie.id,
                persisted_movie.movie_number,
            )
            return persisted_movie
        finally:
            if not finalized:
                self._cleanup_prepared_image_files(prepared_files)

    def sync_movie_desc(self, movie: Movie) -> bool:
        lock_context = self.persist_lock or nullcontext()
        try:
            with lock_context:
                self._mark_movie_desc_fetch_started(movie)
            movie_desc = self.dmm_provider.get_movie_desc(movie.movie_number)
            with lock_context:
                self._mark_movie_desc_fetch_succeeded(movie, movie_desc)
            return True
        except Exception as exc:
            # DMM 已明确确认不存在该番号时，直接标记为终态失败，避免后续自动任务反复重试。
            is_terminal = False
            with lock_context:
                self._mark_movie_desc_fetch_failed(movie, str(exc), terminal=is_terminal)
            logger.warning(
                "Catalog movie desc fetch failed movie_id={} movie_number={} terminal={} detail={}",
                movie.id,
                movie.movie_number,
                is_terminal,
                exc,
            )
            return False

    @classmethod
    def _mark_movie_desc_fetch_started(cls, movie: Movie) -> None:
        ResourceTaskStateService.mark_started(cls.TASK_KEY, movie.id)

    def _refresh_movie_metadata_records_strict(
        self,
        *,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        actors: List[JavdbMovieActorResource],
        tags: List,
        thin_cover_resolution: ThinCoverResolution,
        cover_task: Optional[ImagePersistTask],
        plot_tasks: List[ImagePersistTask],
        actor_image_tasks_by_javdb_id: Dict[str, ImagePersistTask],
    ) -> tuple[Movie, set[str]]:
        movie = Movie.get_by_id(movie.id)
        obsolete_paths: set[str] = set()

        old_cover_image = movie.cover_image
        old_thin_cover_image = movie.thin_cover_image
        if old_cover_image is not None:
            movie.cover_image = None
            movie.save(only=[Movie.cover_image])
        if old_thin_cover_image is not None:
            movie.thin_cover_image = None
            movie.save(only=[Movie.thin_cover_image])

        # 先清空旧剧情图关联，再按远端最新顺序重建，保证详情页严格一致。
        old_plot_links = list(
            MoviePlotImage.select(MoviePlotImage, Image)
            .join(Image)
            .where(MoviePlotImage.movie == movie)
            .order_by(MoviePlotImage.id)
        )
        if old_plot_links:
            MoviePlotImage.delete().where(MoviePlotImage.movie == movie).execute()

        # 演员关系同样按远端列表全量替换，避免残留已下线演员。
        MovieActor.delete().where(MovieActor.movie == movie).execute()
        # 标签关联同样严格重建，保证旧标签不会残留。
        MovieTag.delete().where(MovieTag.movie == movie).execute()

        images_to_cleanup: Dict[int, Image] = {}
        for image in [old_cover_image, old_thin_cover_image, *[plot_link.image for plot_link in old_plot_links]]:
            if image is None:
                continue
            images_to_cleanup[int(image.id)] = image
        for image in images_to_cleanup.values():
            obsolete_paths.update(self._delete_image_record_if_unused(image))

        movie.release_date = detail.release_date
        movie.duration_minutes = detail.duration_minutes or 0
        movie.score = detail.score or 0
        movie.score_number = detail.score_number
        movie.watched_count = detail.watched_count
        movie.want_watch_count = detail.want_watch_count
        movie.comment_count = detail.comment_count
        movie.summary = detail.summary
        movie.series = self._resolve_movie_series(detail.series_name)
        movie.maker_name = detail.maker_name
        movie.director_name = detail.director_name
        movie.extra = detail.extra
        movie.javdb_id = detail.javdb_id
        movie.title = detail.title
        movie.cover_image = self._persist_refreshed_image_record(cover_task)
        movie.save()

        seen_actor_ids: set[str] = set()
        for actor_resource in actors:
            if actor_resource.javdb_id in seen_actor_ids:
                continue
            seen_actor_ids.add(actor_resource.javdb_id)
            actor, actor_obsolete_paths = self._refresh_actor_from_javdb_resource_strict(
                actor_resource=actor_resource,
                profile_image_task=actor_image_tasks_by_javdb_id.get(actor_resource.javdb_id),
            )
            obsolete_paths.update(actor_obsolete_paths)
            MovieActor.get_or_create(movie=movie, actor=actor)

        seen_tag_names: set[str] = set()
        for tag_resource in tags:
            normalized_tag_name = (tag_resource.name or "").strip()
            if not normalized_tag_name or normalized_tag_name in seen_tag_names:
                continue
            seen_tag_names.add(normalized_tag_name)
        for tag_name in seen_tag_names:
            tag, _ = Tag.get_or_create(name=tag_name)
            MovieTag.get_or_create(movie=movie, tag=tag)

        plot_images_by_index: Dict[int, Image] = {}
        for plot_task in plot_tasks:
            plot_image = self._persist_refreshed_image_record(plot_task)
            if plot_image is None:
                continue
            if plot_task.plot_index is not None:
                plot_images_by_index[int(plot_task.plot_index)] = plot_image
            MoviePlotImage.get_or_create(movie=movie, image=plot_image)
        obsolete_paths.update(
            self._apply_thin_cover_resolution(
                movie,
                None,
                thin_cover_resolution,
                plot_images_by_index,
                refreshed=True,
            )
        )

        return movie, obsolete_paths

    def _refresh_actor_from_javdb_resource_strict(
        self,
        *,
        actor_resource: JavdbMovieActorResource,
        profile_image_task: Optional[ImagePersistTask],
    ) -> tuple[Actor, set[str]]:
        actor = Actor.get_or_none(Actor.javdb_id == actor_resource.javdb_id)
        if actor is None:
            profile_image = self._persist_refreshed_image_record(profile_image_task)
            merged_alias_name = self._merge_actor_alias_name(
                primary_name=actor_resource.name,
                alias_names=actor_resource.alias_names,
                existing_alias_name="",
            )
            return (
                Actor.create(
                    javdb_id=actor_resource.javdb_id,
                    name=actor_resource.name,
                    alias_name=merged_alias_name,
                    profile_image=profile_image,
                    javdb_type=actor_resource.javdb_type,
                    gender=actor_resource.gender,
                ),
                set(),
            )

        old_profile_image = actor.profile_image
        actor.profile_image = None
        actor.save(only=[Actor.profile_image])
        obsolete_paths: set[str] = set()
        if old_profile_image is not None:
            obsolete_paths.update(self._delete_image_record_if_unused(old_profile_image))

        profile_image = self._persist_refreshed_image_record(profile_image_task)
        actor.name = actor_resource.name
        actor.alias_name = self._merge_actor_alias_name(
            primary_name=actor_resource.name,
            alias_names=actor_resource.alias_names,
            existing_alias_name=actor.alias_name,
        )
        actor.javdb_type = actor_resource.javdb_type
        actor.gender = actor_resource.gender
        actor.profile_image = profile_image
        actor.save()
        return actor, obsolete_paths

    @classmethod
    def _delete_obsolete_image_files(cls, relative_paths: set[str]) -> None:
        ImageCleanupService.delete_obsolete_image_files(relative_paths)

    def backfill_movie_thin_cover(self, movie: Movie) -> bool:
        """基于已落盘的封面和剧情图，为历史影片补算竖封面图。"""
        lock_context = self.persist_lock or nullcontext()
        obsolete_paths: set[str] = set()
        with lock_context:
            with get_database().atomic():
                movie = Movie.get_by_id(movie.id)
                old_thin_cover_image = movie.thin_cover_image
                plot_links = list(
                    MoviePlotImage.select(MoviePlotImage, Image)
                    .join(Image)
                    .where(MoviePlotImage.movie == movie)
                    .order_by(MoviePlotImage.id)
                )
                thin_cover_resolution = self._resolve_thin_cover_from_existing_movie(movie, plot_links)
                plot_images_by_index = {
                    plot_index: plot_link.image
                    for plot_index, plot_link in enumerate(plot_links)
                }
                obsolete_paths.update(
                    self._apply_thin_cover_resolution(
                        movie,
                        old_thin_cover_image,
                        thin_cover_resolution,
                        plot_images_by_index,
                        refreshed=False,
                    )
                )
        self._delete_obsolete_image_files(obsolete_paths)
        refreshed_movie = Movie.get_by_id(movie.id)
        return refreshed_movie.thin_cover_image_id is not None

    def _download_image_tasks_to_temporary_files(
        self,
        image_tasks: List[ImagePersistTask],
    ) -> List[PreparedImageFile]:
        if not image_tasks:
            return []

        image_root = self._image_root_path()
        image_root.mkdir(parents=True, exist_ok=True)
        temp_root = Path(tempfile.mkdtemp(prefix="catalog-refresh-", dir=str(image_root)))
        prepared_files = [
            PreparedImageFile(
                image_task=image_task,
                temp_path=temp_root / image_task.relative_path,
                temp_root=temp_root,
            )
            for image_task in image_tasks
        ]

        try:
            with ThreadPoolExecutor(max_workers=len(prepared_files), thread_name_prefix="catalog-refresh-image") as executor:
                future_map = {
                    executor.submit(self._download_prepared_image_file, prepared_file): prepared_file
                    for prepared_file in prepared_files
                }
                for future in as_completed(future_map):
                    future.result()
        except Exception:
            self._cleanup_prepared_image_files(prepared_files)
            raise
        return prepared_files

    def _download_prepared_image_file(self, prepared_file: PreparedImageFile) -> None:
        prepared_file.temp_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_downloader(prepared_file.image_task.image_url, prepared_file.temp_path)

    @staticmethod
    def _cleanup_prepared_image_files(prepared_files: List[PreparedImageFile]) -> None:
        for temp_root in {prepared_file.temp_root for prepared_file in prepared_files}:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _finalize_prepared_image_files(self, prepared_files: List[PreparedImageFile]) -> None:
        for prepared_file in prepared_files:
            final_path = prepared_file.image_task.absolute_path
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prepared_file.temp_path, final_path)

        for temp_root in {prepared_file.temp_root for prepared_file in prepared_files}:
            shutil.rmtree(temp_root, ignore_errors=True)

    def _delete_image_record_if_unused(self, image: Image) -> set[str]:
        return ImageCleanupService.delete_image_record_if_unused(image)

    @staticmethod
    def _image_record_is_still_used(image: Image) -> bool:
        return ImageCleanupService.image_record_is_still_used(image)

    @classmethod
    def _mark_movie_desc_fetch_succeeded(cls, movie: Movie, movie_desc: str) -> None:
        movie.desc = movie_desc
        movie.save(only=[Movie.desc])
        ResourceTaskStateService.mark_succeeded(cls.TASK_KEY, movie.id)

    @classmethod
    def _mark_movie_desc_fetch_failed(cls, movie: Movie, detail: str, *, terminal: bool = False) -> None:
        ResourceTaskStateService.mark_failed(
            cls.TASK_KEY,
            movie.id,
            detail,
            extra_patch={"terminal": terminal},
        )

    def upsert_actor_from_javdb_resource(
        self,
        actor_resource: JavdbMovieActorResource,
        profile_image_task: Optional[ImagePersistTask] = None,
    ) -> Actor:
        """把 JavDB 演员资源同步到本地，并在可用时补全头像。"""
        if profile_image_task is None:
            profile_image = self._persist_image(
                owner_type="actor",
                owner_key=actor_resource.javdb_id,
                image_url=actor_resource.avatar_url,
            )
        else:
            profile_image = self._persist_prepared_image(profile_image_task)

        lock_context = self.persist_lock or nullcontext()
        with lock_context:
            with get_database().atomic():
                merged_alias_name = self._merge_actor_alias_name(
                    primary_name=actor_resource.name,
                    alias_names=actor_resource.alias_names,
                    existing_alias_name="",
                )
                actor, created = Actor.get_or_create(
                    javdb_id=actor_resource.javdb_id,
                    defaults={
                        "name": actor_resource.name,
                        "alias_name": merged_alias_name,
                        "profile_image": profile_image,
                        "javdb_type": actor_resource.javdb_type,
                        "gender": actor_resource.gender,
                    },
                )
                if not created:
                    actor.name = actor_resource.name
                    # 只合并权威来源给出的名字集合，避免把用户输入直接污染到 alias。
                    actor.alias_name = self._merge_actor_alias_name(
                        primary_name=actor_resource.name,
                        alias_names=actor_resource.alias_names,
                        existing_alias_name=actor.alias_name,
                    )
                    actor.javdb_type = actor_resource.javdb_type
                    actor.gender = actor_resource.gender
                    if profile_image is not None:
                        actor.profile_image = profile_image
                    actor.save()

        return actor

    def _persist_image(
        self,
        owner_type: str,
        owner_key: str,
        image_url: str | None,
        plot_index: Optional[int] = None,
    ) -> Image | None:
        """为单张图片执行“准备路径 -> 下载文件 -> upsert 图片记录”三步。"""
        image_task = self._build_image_task(
            owner_type=owner_type,
            owner_key=owner_key,
            image_url=image_url,
            plot_index=plot_index,
        )
        if image_task is None:
            return None

        image_task.absolute_path.parent.mkdir(parents=True, exist_ok=True)
        if not image_task.absolute_path.exists():
            logger.debug("Persist image downloading url={} target={}", image_task.image_url, str(image_task.absolute_path))
            self.image_downloader(image_task.image_url, image_task.absolute_path)
        else:
            logger.debug("Persist image reused local file path={}", str(image_task.absolute_path))

        return self._persist_prepared_image(image_task)

    def _build_movie_image_tasks(
        self, movie_number: str, cover_image_url: str | None, plot_urls: List[str]
    ) -> Tuple[Optional[ImagePersistTask], List[ImagePersistTask]]:
        """统一生成影片封面和剧照的本地落盘任务。"""
        cover_task = self._build_image_task(
            owner_type="movie_cover",
            owner_key=movie_number,
            image_url=cover_image_url,
        )
        plot_tasks: List[ImagePersistTask] = []
        for image_index, plot_url in enumerate(plot_urls):
            image_task = self._build_image_task(
                owner_type="movie_plot",
                owner_key=movie_number,
                image_url=plot_url,
                plot_index=image_index,
            )
            if image_task is not None:
                plot_tasks.append(image_task)
        return cover_task, plot_tasks

    def _build_movie_import_image_tasks(
        self,
        movie_number: str,
        cover_image_url: str | None,
        plot_urls: List[str],
        actors: List[JavdbMovieActorResource],
    ) -> Tuple[Optional[ImagePersistTask], List[ImagePersistTask], Dict[str, ImagePersistTask]]:
        cover_task, plot_tasks = self._build_movie_image_tasks(movie_number, cover_image_url, plot_urls)
        actor_image_tasks_by_javdb_id: Dict[str, ImagePersistTask] = {}
        for actor_resource in actors:
            if actor_resource.javdb_id in actor_image_tasks_by_javdb_id:
                continue
            image_task = self._build_image_task(
                owner_type="actor",
                owner_key=actor_resource.javdb_id,
                image_url=actor_resource.avatar_url,
            )
            if image_task is None:
                continue
            actor_image_tasks_by_javdb_id[actor_resource.javdb_id] = image_task
        return cover_task, plot_tasks, actor_image_tasks_by_javdb_id

    def _collect_image_tasks(
        self,
        cover_task: Optional[ImagePersistTask],
        plot_tasks: List[ImagePersistTask],
        actor_image_tasks_by_javdb_id: Dict[str, ImagePersistTask],
    ) -> List[ImagePersistTask]:
        tasks: List[ImagePersistTask] = []
        seen_relative_paths: set[str] = set()
        for image_task in [cover_task, *plot_tasks, *actor_image_tasks_by_javdb_id.values()]:
            if image_task is None or image_task.relative_path in seen_relative_paths:
                continue
            seen_relative_paths.add(image_task.relative_path)
            tasks.append(image_task)
        return tasks

    def _build_image_task(
        self,
        owner_type: str,
        owner_key: str,
        image_url: str | None,
        plot_index: Optional[int] = None,
    ) -> Optional[ImagePersistTask]:
        """把远端图片 URL 解析成稳定的本地相对路径和绝对路径。"""
        if not image_url:
            logger.debug(
                "Persist image skipped because url is empty owner_type={} owner_key={}",
                owner_type,
                owner_key,
            )
            return None

        safe_owner_key = self._normalize_owner_key(owner_key)
        extension = self._normalize_image_extension(Path(urlparse(image_url).path).suffix)

        if owner_type == "actor":
            relative_path = (Path("actors") / f"{safe_owner_key}{extension}").as_posix()
            image_type = "actor"
        elif owner_type == "movie_cover":
            relative_path = (Path("movies") / safe_owner_key / f"cover{extension}").as_posix()
            image_type = "cover"
        elif owner_type == "movie_plot":
            if plot_index is None:
                raise ValueError("plot_index is required for movie plot images")
            relative_path = (
                Path("movies") / safe_owner_key / "plots" / f"{plot_index}{extension}"
            ).as_posix()
            image_type = "plot"
        else:
            raise ValueError(f"unsupported owner_type: {owner_type}")

        absolute_path = self._image_root_path() / relative_path
        return ImagePersistTask(
            image_type=image_type,
            image_url=image_url,
            relative_path=relative_path,
            absolute_path=absolute_path,
            plot_index=plot_index,
        )

    def _download_image_tasks(self, image_tasks: List[ImagePersistTask]) -> None:
        """并发下载一批图片；封面失败会中断导入，剧情图/头像失败仅告警跳过。"""
        tasks_to_download: List[ImagePersistTask] = []
        for image_task in image_tasks:
            image_task.absolute_path.parent.mkdir(parents=True, exist_ok=True)
            if image_task.absolute_path.exists():
                logger.debug(
                    "Catalog image download reused local file type={} path={}",
                    image_task.image_type,
                    str(image_task.absolute_path),
                )
                continue
            tasks_to_download.append(image_task)

        if not tasks_to_download:
            return

        cover_download_errors: List[ImageDownloadError] = []
        with ThreadPoolExecutor(max_workers=len(tasks_to_download), thread_name_prefix="catalog-image") as executor:
            future_map = {executor.submit(self._download_movie_image_task, task): task for task in tasks_to_download}
            for future in as_completed(future_map):
                image_task = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    # 仅封面下载失败会中断影片导入；剧情图和演员头像失败只记录告警并继续。
                    if image_task.image_type != "cover":
                        logger.warning(
                            "Catalog image download skipped after failure image_type={} url={} target={} detail={}",
                            image_task.image_type,
                            image_task.image_url,
                            str(image_task.absolute_path),
                            exc,
                        )
                        continue
                    logger.warning(
                        "Catalog cover image download failed image_type={} url={} target={} detail={}",
                        image_task.image_type,
                        image_task.image_url,
                        str(image_task.absolute_path),
                        exc,
                    )
                    if isinstance(exc, ImageDownloadError):
                        cover_download_errors.append(exc)
                    else:
                        cover_download_errors.append(ImageDownloadError(f"download_failed:{image_task.image_url}:{exc}"))

        if cover_download_errors:
            raise cover_download_errors[0]

    def _download_movie_image_task(self, image_task: ImagePersistTask) -> None:
        logger.debug(
            "Catalog image download scheduled type={} url={} target={}",
            image_task.image_type,
            image_task.image_url,
            str(image_task.absolute_path),
        )
        self.image_downloader(image_task.image_url, image_task.absolute_path)

    def _persist_prepared_image(self, image_task: Optional[ImagePersistTask]) -> Image | None:
        if image_task is None:
            return None
        # 非致命图片下载失败时不会落地文件，这里直接跳过数据库记录，避免脏路径。
        if not image_task.absolute_path.exists():
            logger.warning(
                "Persist image skipped because local file is missing image_type={} url={} target={}",
                image_task.image_type,
                image_task.image_url,
                str(image_task.absolute_path),
            )
            return None
        return self._upsert_image_record(image_task.relative_path)

    def _persist_refreshed_image_record(self, image_task: Optional[ImagePersistTask]) -> Image | None:
        if image_task is None:
            return None
        # 严格刷新场景的新图片还在临时目录中，事务内只需要先切换到目标相对路径。
        return self._upsert_image_record(image_task.relative_path)

    def _upsert_image_record(self, relative_path: str) -> Image:
        """确保同一路径只存在一条 Image 记录，并把 small/medium/large 统一到该路径。"""
        image = Image.get_or_none(Image.origin == relative_path)
        if image is None:
            logger.debug("Persist image creating record relative_path={}", relative_path)
            return Image.create(
                origin=relative_path,
                small=relative_path,
                medium=relative_path,
                large=relative_path,
            )

        if image.small != relative_path or image.medium != relative_path or image.large != relative_path:
            image.small = relative_path
            image.medium = relative_path
            image.large = relative_path
            image.save()
            logger.debug("Persist image normalized variants image_id={} relative_path={}", image.id, relative_path)
        else:
            logger.debug("Persist image record exists image_id={} relative_path={}", image.id, relative_path)
        return image

    @staticmethod
    def _image_root_path() -> Path:
        """导入图片根目录支持相对路径配置，统一在这里解析成绝对路径。"""
        image_root_path = Path(settings.media.import_image_root_path).expanduser()
        if not image_root_path.is_absolute():
            image_root_path = (Path.cwd() / image_root_path).resolve()
        return image_root_path

    def _unique_preserve_order(self, items: List[str]) -> List[str]:
        """在保留 JavDB 原始顺序的前提下去重。"""
        unique_items: List[str] = []
        seen_items = set()
        for item in items:
            if item in seen_items:
                continue
            seen_items.add(item)
            unique_items.append(item)
        return unique_items

    def _download_image(self, image_url: str, target_path: Path) -> None:
        """下载单张图片并带有限次重试；失败时抛 ImageDownloadError。"""
        if target_path.exists():
            logger.debug("Import image download skipped because local file exists path={}", str(target_path))
            return

        last_error: Exception | None = None
        for attempt in range(1, self.IMAGE_DOWNLOAD_MAX_RETRIES + 1):
            logger.debug(
                "Import image download start url={} target={} attempt={}/{}",
                image_url,
                str(target_path),
                attempt,
                self.IMAGE_DOWNLOAD_MAX_RETRIES,
            )
            try:
                response = self.http_client.request("GET", image_url)
                if response.status_code != 200:
                    raise ImageDownloadError(f"unexpected_status_code:{response.status_code}")

                target_path.write_bytes(response.content)
                logger.debug(
                    "Import image download success url={} target={} size_bytes={} attempt={}",
                    image_url,
                    str(target_path),
                    len(response.content),
                    attempt,
                )
                return
            except (httpx.HTTPError, ImageDownloadError) as exc:
                last_error = exc
                logger.warning(
                    "Import image download failed url={} target={} attempt={}/{} detail={}",
                    image_url,
                    str(target_path),
                    attempt,
                    self.IMAGE_DOWNLOAD_MAX_RETRIES,
                    exc,
                )
                if attempt < self.IMAGE_DOWNLOAD_MAX_RETRIES:
                    time.sleep(min(0.3 * attempt, 1.0))

        raise ImageDownloadError(f"download_failed:{image_url}:{last_error}")
