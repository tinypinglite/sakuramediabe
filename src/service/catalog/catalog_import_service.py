"""目录导入 service。

负责把 JavDB 返回的影片/演员详情转换成本地目录数据，并处理图片下载与图片记录持久化。
阅读入口建议从 ``upsert_movie_from_javdb_detail`` 开始，再看图片任务的构建、下载和入库 helper。
"""

from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
from loguru import logger

from src.config.config import settings
from src.model import Actor, Image, Movie, MovieActor, MoviePlotImage, MovieTag, Tag, get_database
from src.schema.metadata.javdb import JavdbMovieActorResource, JavdbMovieDetailResource
from src.service.catalog.movie_collection_service import MovieCollectionService


class ImageDownloadError(Exception):
    pass


@dataclass
class ImagePersistTask:
    image_type: str
    image_url: str
    relative_path: str
    absolute_path: Path
    plot_index: Optional[int] = None


class CatalogImportService:
    """承接远端元数据到本地目录模型的 upsert。"""
    # 图片下载重试次数
    IMAGE_DOWNLOAD_MAX_RETRIES = 6
    # 图片下载超时秒数
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        image_downloader: Callable[[str, Path], None] | None = None,
        persist_lock=None,
    ):
        self.http_client = httpx.Client(
            timeout=self.IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
            trust_env=False,
        )
        self.image_downloader = image_downloader or self._download_image
        self.persist_lock = persist_lock

    def upsert_movie_from_javdb_detail(
        self,
        detail: JavdbMovieDetailResource,
        force_subscribed: bool = False,
    ) -> Movie:
        """把一份 JavDB 影片详情完整落到本地 Movie/Actor/Tag/Image 关系中。"""
        logger.info(
            "Catalog upsert start movie_number={} javdb_id={} actors={} tags={} plot_images={}",
            detail.movie_number,
            detail.javdb_id,
            len(detail.actors),
            len(detail.tags),
            len(detail.plot_images),
        )
        plot_urls = self._unique_preserve_order(detail.plot_images)
        if len(plot_urls) != len(detail.plot_images):
            logger.debug(
                "Catalog upsert deduplicated plot images movie_number={} original={} deduplicated={}",
                detail.movie_number,
                len(detail.plot_images),
                len(plot_urls),
            )

        # 影片保存进事务前先把全部图片准备好并下载完成，避免事务里做慢速网络 IO。
        cover_task, plot_tasks, actor_image_tasks_by_javdb_id = self._build_movie_import_image_tasks(
            detail.movie_number,
            detail.cover_image,
            plot_urls,
            detail.actors,
        )
        self._download_image_tasks(
            self._collect_image_tasks(
                cover_task,
                plot_tasks,
                actor_image_tasks_by_javdb_id,
            )
        )

        lock_context = self.persist_lock or nullcontext()
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
                movie.series_name = detail.series_name
                if target_is_subscribed is not None:
                    movie.is_subscribed = target_is_subscribed
                    if target_is_subscribed:
                        if not was_subscribed or movie.subscribed_at is None:
                            movie.subscribed_at = datetime.utcnow()
                    else:
                        movie.subscribed_at = None
                movie.extra = detail.extra
                movie.title = detail.title
                movie.javdb_id = detail.javdb_id
                movie.movie_number = detail.movie_number
                movie.is_collection = MovieCollectionService.matches_configured_collection(detail.movie_number)
                movie.save()
                logger.debug(
                    "Catalog upsert movie saved movie_id={} movie_number={} created={}",
                    movie.id,
                    movie.movie_number,
                    created_movie,
                )

                # 演员、标签、剧照关系都使用 get_or_create，避免多次导入产生重复关联。
                for actor_resource in detail.actors:
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

                for tag_resource in detail.tags:
                    tag, _ = Tag.get_or_create(name=tag_resource.name)
                    MovieTag.get_or_create(movie=movie, tag=tag)
                    logger.debug("Catalog upsert tag linked movie_id={} tag_id={} tag_name={}", movie.id, tag.id, tag.name)

                for plot_task in plot_tasks:
                    plot_image = self._persist_prepared_image(plot_task)
                    if plot_image is not None:
                        MoviePlotImage.get_or_create(movie=movie, image=plot_image)
                        logger.debug(
                            "Catalog upsert plot image linked movie_id={} image_id={} index={}",
                            movie.id,
                            plot_image.id,
                            plot_task.plot_index,
                        )

        logger.info("Catalog upsert finished movie_id={} movie_number={}", movie.id, movie.movie_number)
        return movie

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
                actor, created = Actor.get_or_create(
                    javdb_id=actor_resource.javdb_id,
                    defaults={
                        "name": actor_resource.name,
                        "alias_name": actor_resource.name,
                        "profile_image": profile_image,
                        "javdb_type": actor_resource.javdb_type,
                        "gender": actor_resource.gender,
                    },
                )
                if not created:
                    actor.name = actor_resource.name
                    if not actor.alias_name:
                        actor.alias_name = actor_resource.name
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

        safe_owner_key = re.sub(r"[^0-9A-Za-z._-]", "_", owner_key).strip("._-") or "unknown"
        extension = Path(urlparse(image_url).path).suffix or ".jpg"
        if len(extension) > 8:
            extension = ".jpg"
        extension = extension.lower()

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
        """并发下载一批图片；任一任务失败都会让整次影片导入失败。"""
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

        download_errors: List[ImageDownloadError] = []
        with ThreadPoolExecutor(max_workers=len(tasks_to_download), thread_name_prefix="catalog-image") as executor:
            future_map = {executor.submit(self._download_movie_image_task, task): task for task in tasks_to_download}
            for future in as_completed(future_map):
                image_task = future_map[future]
                try:
                    future.result()
                except Exception as exc:
                    logger.warning(
                        "Catalog image download task failed image_type={} url={} target={} detail={}",
                        image_task.image_type,
                        image_task.image_url,
                        str(image_task.absolute_path),
                        exc,
                    )
                    if isinstance(exc, ImageDownloadError):
                        download_errors.append(exc)
                    else:
                        download_errors.append(ImageDownloadError(f"download_failed:{image_task.image_url}:{exc}"))

        if download_errors:
            raise download_errors[0]

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

    def _image_root_path(self) -> Path:
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
