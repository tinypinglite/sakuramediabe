"""影片图片子系统 service。

从 ``CatalogImportService`` 抽出，专门负责影片/演员图片的下载、封面切割、薄封面解析
与 Image 记录持久化。本 service 只处理图片，不写 Movie 元数据：薄封面记录由本服务
持久化并返回，``Movie.thin_cover_image`` 的回写留在编排层（CatalogImportService）。

阅读入口建议从图片任务的构建（``build_movie_import_image_tasks``）开始，再看下载、
薄封面解析（``resolve_thin_cover_*``）和入库 helper（``persist_*``）。
"""

import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from loguru import logger
from peewee import EXCLUDED
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError

from src.common.media_paths import (
    media_image_root_path,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.common.runtime_time import utc_now_for_db
from src.metadata._providers.models import JavdbMovieActorResource
from src.model import Image, Movie, MoviePlotImage
from src.service.catalog.image_cleanup_service import ImageCleanupService


class ImageDownloadError(Exception):
    pass


@dataclass
class ImagePersistTask:
    image_type: str
    image_url: str
    relative_path: str
    absolute_path: Path
    plot_index: int | None = None


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


class MovieImageService:
    """影片图片下载、封面切割与 Image 记录持久化。"""

    # 图片下载重试次数
    IMAGE_DOWNLOAD_MAX_RETRIES = 6
    # 图片下载超时秒数
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 30

    def __init__(self, image_downloader: Callable[[str, Path], None] | None = None):
        # http_client 急切构造：下载单测会在实例上 monkeypatch http_client.request，
        # 懒建会让 patch 定位不到真正发请求的 client。
        self.http_client = httpx.Client(
            timeout=self.IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
            trust_env=False,
        )
        self.image_downloader = image_downloader or self._download_image

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
        safe_owner_key = normalize_asset_dir_name(movie_number)
        normalized_extension = self._normalize_image_extension(extension)
        relative_path = (
            movie_asset_relative_dir(safe_owner_key) / f"thin-cover{normalized_extension}"
        ).as_posix()
        absolute_path = media_image_root_path() / relative_path
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

    def _select_portrait_plot_index(self, plot_items: list[tuple[int, Path]]) -> int | None:
        # 业务约定只允许前两张剧情图参与竖封面回退，后续剧情图不再参与判定。
        for plot_index, plot_path in plot_items[:2]:
            if self._is_portrait_image(plot_path):
                return plot_index
        return None

    def resolve_thin_cover_from_downloaded_images(
        self,
        movie_number: str,
        cover_task: ImagePersistTask | None,
        plot_tasks: list[ImagePersistTask],
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

    def resolve_thin_cover_from_prepared_images(
        self,
        movie_number: str,
        cover_task: ImagePersistTask | None,
        plot_tasks: list[ImagePersistTask],
        prepared_files: list[PreparedImageFile],
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

    def resolve_thin_cover_from_existing_movie(
        self,
        movie: Movie,
        plot_links: list[MoviePlotImage],
    ) -> ThinCoverResolution:
        cover_image = movie.cover_image
        if cover_image is not None:
            cover_path = media_image_root_path() / cover_image.origin
            thin_cover_task = self._generate_thin_cover_task_from_cover(
                movie.movie_number,
                cover_path,
                Path(cover_image.origin).suffix,
            )
            if thin_cover_task is not None:
                return ThinCoverResolution(generated_task=thin_cover_task)
        selected_plot_index = self._select_portrait_plot_index(
            [
                (plot_index, media_image_root_path() / plot_link.image.origin)
                for plot_index, plot_link in enumerate(plot_links)
            ]
        )
        return ThinCoverResolution(selected_plot_index=selected_plot_index)

    def resolve_persisted_thin_cover_image(
        self,
        resolution: ThinCoverResolution,
        plot_images_by_index: dict[int, Image],
        *,
        refreshed: bool,
    ) -> Image | None:
        """把薄封面解析结果持久化成 Image 记录并返回；不写 Movie，由编排层回写。"""
        if resolution.generated_task is not None:
            return (
                self.persist_refreshed_image_record(resolution.generated_task)
                if refreshed
                else self.persist_prepared_image(resolution.generated_task)
            )
        if resolution.selected_plot_index is not None:
            return plot_images_by_index.get(resolution.selected_plot_index)
        return None

    def build_movie_import_image_tasks(
        self,
        movie_number: str,
        cover_image_url: str | None,
        plot_urls: list[str],
        actors: list[JavdbMovieActorResource],
    ) -> tuple[ImagePersistTask | None, list[ImagePersistTask], dict[str, ImagePersistTask]]:
        cover_task, plot_tasks = self._build_movie_image_tasks(movie_number, cover_image_url, plot_urls)
        actor_image_tasks_by_javdb_id: dict[str, ImagePersistTask] = {}
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

    def _build_movie_image_tasks(
        self, movie_number: str, cover_image_url: str | None, plot_urls: list[str]
    ) -> tuple[ImagePersistTask | None, list[ImagePersistTask]]:
        """统一生成影片封面和剧照的本地落盘任务。"""
        cover_task = self._build_image_task(
            owner_type="movie_cover",
            owner_key=movie_number,
            image_url=cover_image_url,
        )
        plot_tasks: list[ImagePersistTask] = []
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

    def _build_image_task(
        self,
        owner_type: str,
        owner_key: str,
        image_url: str | None,
        plot_index: int | None = None,
    ) -> ImagePersistTask | None:
        """把远端图片 URL 解析成稳定的本地相对路径和绝对路径。"""
        if not image_url:
            logger.debug(
                "Persist image skipped because url is empty owner_type={} owner_key={}",
                owner_type,
                owner_key,
            )
            return None

        safe_owner_key = normalize_asset_dir_name(owner_key)
        extension = self._normalize_image_extension(Path(urlparse(image_url).path).suffix)

        if owner_type == "actor":
            relative_path = (Path("actors") / f"{safe_owner_key}{extension}").as_posix()
            image_type = "actor"
        elif owner_type == "movie_cover":
            relative_path = (
                movie_asset_relative_dir(safe_owner_key) / f"cover{extension}"
            ).as_posix()
            image_type = "cover"
        elif owner_type == "movie_plot":
            if plot_index is None:
                raise ValueError("plot_index is required for movie plot images")
            # 剧照与 cover/thin-cover 同层平铺，避免 30w 规模下多出 30w 个空 plots/ 中间目录。
            relative_path = (
                movie_asset_relative_dir(safe_owner_key) / f"plot-{plot_index}{extension}"
            ).as_posix()
            image_type = "plot"
        else:
            raise ValueError(f"unsupported owner_type: {owner_type}")

        absolute_path = media_image_root_path() / relative_path
        return ImagePersistTask(
            image_type=image_type,
            image_url=image_url,
            relative_path=relative_path,
            absolute_path=absolute_path,
            plot_index=plot_index,
        )

    def collect_image_tasks(
        self,
        cover_task: ImagePersistTask | None,
        plot_tasks: list[ImagePersistTask],
        actor_image_tasks_by_javdb_id: dict[str, ImagePersistTask],
    ) -> list[ImagePersistTask]:
        tasks: list[ImagePersistTask] = []
        seen_relative_paths: set[str] = set()
        for image_task in [cover_task, *plot_tasks, *actor_image_tasks_by_javdb_id.values()]:
            if image_task is None or image_task.relative_path in seen_relative_paths:
                continue
            seen_relative_paths.add(image_task.relative_path)
            tasks.append(image_task)
        return tasks

    def download_image_tasks(self, image_tasks: list[ImagePersistTask]) -> None:
        """并发下载一批图片；封面失败会中断导入，剧情图/头像失败仅告警跳过。"""
        tasks_to_download: list[ImagePersistTask] = []
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

        cover_download_errors: list[ImageDownloadError] = []
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

    def download_image_tasks_to_temporary_files(
        self,
        image_tasks: list[ImagePersistTask],
    ) -> list[PreparedImageFile]:
        if not image_tasks:
            return []

        image_root = media_image_root_path()
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
            self.cleanup_prepared_image_files(prepared_files)
            raise
        return prepared_files

    def _download_prepared_image_file(self, prepared_file: PreparedImageFile) -> None:
        prepared_file.temp_path.parent.mkdir(parents=True, exist_ok=True)
        self.image_downloader(prepared_file.image_task.image_url, prepared_file.temp_path)

    @staticmethod
    def cleanup_prepared_image_files(prepared_files: list[PreparedImageFile]) -> None:
        for temp_root in {prepared_file.temp_root for prepared_file in prepared_files}:
            shutil.rmtree(temp_root, ignore_errors=True)

    def finalize_prepared_image_files(self, prepared_files: list[PreparedImageFile]) -> None:
        for prepared_file in prepared_files:
            final_path = prepared_file.image_task.absolute_path
            final_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(prepared_file.temp_path, final_path)

        for temp_root in {prepared_file.temp_root for prepared_file in prepared_files}:
            shutil.rmtree(temp_root, ignore_errors=True)

    def persist_image(
        self,
        owner_type: str,
        owner_key: str,
        image_url: str | None,
        plot_index: int | None = None,
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

        return self.persist_prepared_image(image_task)

    def persist_prepared_image(self, image_task: ImagePersistTask | None) -> Image | None:
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

    def persist_prepared_images(
        self,
        image_tasks: Iterable[ImagePersistTask | None],
    ) -> dict[str, Image]:
        """``persist_prepared_image`` 的批量版：一次 upsert 落库整批已下载图片。

        返回 ``relative_path -> Image``；本地文件缺失的任务与单张版一样跳过并告警，
        因此调用方需按 ``relative_path`` 取值并处理 None。
        """
        persistable_paths: list[str] = []
        for image_task in image_tasks:
            if image_task is None:
                continue
            if not image_task.absolute_path.exists():
                logger.warning(
                    "Persist image skipped because local file is missing image_type={} url={} target={}",
                    image_task.image_type,
                    image_task.image_url,
                    str(image_task.absolute_path),
                )
                continue
            persistable_paths.append(image_task.relative_path)
        return self._upsert_image_records(persistable_paths)

    def persist_refreshed_image_record(self, image_task: ImagePersistTask | None) -> Image | None:
        if image_task is None:
            return None
        # 严格刷新场景的新图片还在临时目录中，事务内只需要先切换到目标相对路径。
        return self._upsert_image_record(image_task.relative_path)

    def persist_refreshed_image_records(
        self,
        image_tasks: Iterable[ImagePersistTask | None],
    ) -> dict[str, Image]:
        """``persist_refreshed_image_record`` 的批量版，返回 ``relative_path -> Image``。

        严格刷新场景文件还在临时目录，不做落地校验，直接整批切换到目标相对路径。
        """
        return self._upsert_image_records(
            [image_task.relative_path for image_task in image_tasks if image_task is not None]
        )

    def _upsert_image_record(self, relative_path: str) -> Image:
        """确保同一路径只存在一条 Image 记录，并把 small/medium/large 统一到该路径。"""
        return self._upsert_image_records([relative_path])[relative_path]

    @staticmethod
    def _upsert_image_records(relative_paths: Sequence[str]) -> dict[str, Image]:
        """批量 upsert 图片记录，返回 ``relative_path -> Image`` 映射。

        单条 ``INSERT ... ON CONFLICT (origin) DO UPDATE ... RETURNING`` 覆盖全部路径：
        - 相比逐行 ``get_or_none`` + ``create``，往返次数从 2N 降到 1；
        - ``DO UPDATE`` 而非 ``DO NOTHING``，既保证冲突行也出现在 RETURNING 结果里，
          又顺带把 small/medium/large 归一到 origin，与原逐行实现语义一致；
        - 单语句天然规避原实现 get_or_none 与 create 之间的并发窗口（唯一约束冲突）。
        """
        unique_paths = list(dict.fromkeys(path for path in relative_paths if path))
        if not unique_paths:
            return {}

        now = utc_now_for_db()
        rows = [
            {
                Image.origin: path,
                Image.small: path,
                Image.medium: path,
                Image.large: path,
                Image.created_at: now,
                Image.updated_at: now,
            }
            for path in unique_paths
        ]
        query = (
            Image.insert_many(rows)
            .on_conflict(
                conflict_target=[Image.origin],
                update={
                    Image.small: EXCLUDED.small,
                    Image.medium: EXCLUDED.medium,
                    Image.large: EXCLUDED.large,
                    Image.updated_at: EXCLUDED.updated_at,
                },
            )
            .returning(Image)
        )
        images_by_path = {image.origin: image for image in query.execute()}
        logger.debug(
            "Persist image upserted batch requested={} returned={}",
            len(unique_paths),
            len(images_by_path),
        )
        return images_by_path

    def delete_image_record_if_unused(self, image: Image) -> set[str]:
        return ImageCleanupService.delete_image_record_if_unused(image)

    @classmethod
    def delete_obsolete_image_files(cls, relative_paths: set[str]) -> None:
        ImageCleanupService.delete_obsolete_image_files(relative_paths)

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
