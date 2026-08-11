"""目录导入 service。

负责把 JavDB 返回的影片/演员详情转换成本地目录数据。图片下载与图片记录持久化已抽到
``MovieImageService``，本 service 只做元数据编排，图片相关能力统一委托 ``self.image_service``。
阅读入口建议从 ``upsert_movie_from_javdb_detail`` 开始。
"""

from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.metadata._providers.dmm import (
    DmmMovieDescNotFoundError,
    DmmMovieNumberNotFoundError,
    DmmProvider,
)
from src.metadata._providers.exceptions import MetadataRequestError
from src.metadata._providers.models import (
    JavdbMovieActorResource,
    JavdbMovieDetailResource,
)
from src.model import (
    Actor,
    Image,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieTag,
    Tag,
    get_database,
)
from src.service.catalog.movie_collection_service import MovieCollectionService
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    ImagePersistTask,
    MovieImageService,
    PreparedImageFile,
    ThinCoverResolution,
)
from src.service.system.resource_task_runner import (
    STATE_FAILED_TERMINAL,
    ResourceTaskLedger,
    RetryPolicy,
    TaskAbortError,
    TaskItemError,
)
from src.service.system.resource_task_state_service import ResourceTaskStateService

# 兼容既有导入路径：ImageDownloadError 等类型历史上从本模块导出，且多处 `except ImageDownloadError`
# 依赖同一个类对象，这里显式再导出保证类身份唯一。
__all__ = [
    "CatalogImportService",
    "ImageDownloadError",
    "ImagePersistTask",
    "PreparedImageFile",
    "ThinCoverResolution",
]


class CatalogImportService:
    """承接远端元数据到本地目录模型的 upsert。"""
    TASK_KEY = "movie_desc_sync"

    # DMM 简介为次要补充：连续请求失败到达该阈值即判定 DMM 当前不可用，
    # 本 service 实例后续直接跳过抓取，避免每部影片都重复走超时+重试拖慢同步。
    DMM_UNAVAILABLE_FAILURE_THRESHOLD = 3

    # movie_desc_sync 重试预算（kernel 记账）：网络性失败按小时级指数退避，
    # 5 次后 exhausted；DMM 确认无此番号/无简介为 failed_terminal 不进预算。
    DESC_SYNC_RETRY_POLICY = RetryPolicy(
        max_attempts=5, backoff_base_seconds=3600, backoff_max_seconds=86400
    )

    def __init__(
        self,
        image_downloader: Callable[[str, Path], None] | None = None,
        persist_lock=None,
        dmm_provider: DmmProvider | None = None,
        skip_dmm: bool = False,
    ):
        # 图片子系统统一交由 MovieImageService，downloader 透传下去保住 media_import 的注入接缝。
        self.image_service = MovieImageService(image_downloader=image_downloader)
        self.persist_lock = persist_lock
        self.dmm_provider = dmm_provider or self._build_dmm_provider()
        # DMM 熔断状态：连续连通性失败计数与是否已判定不可用（仅在本实例生命周期内有效）。
        self._dmm_request_failures = 0
        # 插件/批量导入可显式跳过 DMM 简介抓取（每部省 ~1.8s），由调用方按需开启。
        self._dmm_circuit_open = skip_dmm

    @staticmethod
    def _build_dmm_provider() -> DmmProvider:
        from src.metadata.factory import build_dmm_provider
        return build_dmm_provider()

    @staticmethod
    def _split_actor_alias_name(alias_name: str) -> list[str]:
        return [name.strip() for name in (alias_name or "").split("/") if name.strip()]

    @classmethod
    def _merge_actor_alias_name(
        cls,
        primary_name: str,
        alias_names: list[str],
        existing_alias_name: str,
    ) -> str:
        merged_aliases: list[str] = []
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

    def _apply_thin_cover_resolution(
        self,
        movie: Movie,
        old_thin_cover_image: Image | None,
        resolution: ThinCoverResolution,
        plot_images_by_index: dict[int, Image],
        *,
        refreshed: bool,
    ) -> set[str]:
        # 薄封面 Image 记录的持久化交给图片服务，Movie.thin_cover_image 的回写留在编排层，
        # 保证图片服务不写 Movie。
        new_thin_cover_image = self.image_service.resolve_persisted_thin_cover_image(
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
        return self.image_service.delete_image_record_if_unused(old_thin_cover_image)

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
        cover_task, plot_tasks, actor_image_tasks_by_javdb_id = self.image_service.build_movie_import_image_tasks(
            detail.movie_number,
            detail.cover_image,
            plot_urls,
            actors,
        )
        self.image_service.download_image_tasks(
            self.image_service.collect_image_tasks(
                cover_task,
                plot_tasks,
                actor_image_tasks_by_javdb_id,
            )
        )
        thin_cover_resolution = self.image_service.resolve_thin_cover_from_downloaded_images(
            detail.movie_number,
            cover_task,
            plot_tasks,
        )

        lock_context = self.persist_lock or nullcontext()
        obsolete_paths: set[str] = set()
        with lock_context, get_database().atomic():
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
                movie.cover_image = self.image_service.persist_prepared_image(cover_task)
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

            plot_images_by_index: dict[int, Image] = {}
            # 剧照整批一次 upsert，避免逐张 get_or_none + create 的 2N 次往返。
            plot_images_by_path = self.image_service.persist_prepared_images(plot_tasks)
            for plot_task in plot_tasks:
                plot_image = plot_images_by_path.get(plot_task.relative_path)
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

        self.image_service.delete_obsolete_image_files(obsolete_paths)
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
        cover_task, plot_tasks, actor_image_tasks_by_javdb_id = self.image_service.build_movie_import_image_tasks(
            movie.movie_number,
            detail.cover_image,
            plot_urls,
            actors,
        )
        image_tasks = self.image_service.collect_image_tasks(
            cover_task,
            plot_tasks,
            actor_image_tasks_by_javdb_id,
        )

        # 严格刷新先把新图片全部下载到临时目录，避免中途失败污染正式目录。
        prepared_files = self.image_service.download_image_tasks_to_temporary_files(image_tasks)
        thin_cover_resolution = self.image_service.resolve_thin_cover_from_prepared_images(
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
            with lock_context, get_database().atomic():
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
            self.image_service.finalize_prepared_image_files(prepared_files)
            self.image_service.delete_obsolete_image_files(obsolete_paths - new_relative_paths)
            finalized = True
            logger.info(
                "Catalog strict metadata refresh finished movie_id={} movie_number={}",
                persisted_movie.id,
                persisted_movie.movie_number,
            )
            return persisted_movie
        finally:
            if not finalized:
                self.image_service.cleanup_prepared_image_files(prepared_files)

    def sync_movie_desc(self, movie: Movie) -> bool:
        """公共入口（热评同步、影片导入等 upsert 链路复用）：kernel 记账的单资源执行。"""
        task_state = ResourceTaskStateService.get_state(self.TASK_KEY, movie.id)
        if task_state is not None and task_state.state == STATE_FAILED_TERMINAL:
            # 终态必须在公共入口统一拦截，避免 upsert 链路绕过候选过滤反复请求 DMM。
            logger.info(
                "Catalog movie desc sync skipped terminal failure movie_id={} movie_number={}",
                movie.id,
                movie.movie_number,
            )
            return False

        # DMM 已在本实例生命周期内判定不可用：直接跳过，不发请求也不消耗预算，
        # 状态保持原样，留待网络恢复后的定时任务补抓。
        if self._dmm_circuit_open:
            return False

        from src.service.system.activity_service import ActivityService

        run_context = ActivityService.get_task_run_context()
        lock_context = self.persist_lock or nullcontext()
        with lock_context:
            claim = ResourceTaskLedger.begin_attempt(
                task_key=self.TASK_KEY,
                resource_type="movie",
                resource_id=movie.id,
                trigger_type=getattr(run_context, "trigger_type", None),
                task_run_id=getattr(run_context, "task_run_id", None),
            )
        if claim is None:
            # 行级领取失败：该影片正被其它 run（批跑/子集跑）抓取中，本次跳过。
            logger.info(
                "Catalog movie desc sync skipped, movie busy in another run movie_id={} movie_number={}",
                movie.id,
                movie.movie_number,
            )
            return False
        attempt, record, _prior_state = claim
        try:
            movie_desc = self.fetch_movie_desc_strict(movie)
        except TaskItemError as exc:
            with lock_context:
                ResourceTaskLedger.finish_failure(
                    attempt,
                    record,
                    error_code=exc.error_code,
                    error_message=str(exc),
                    retryable=exc.retryable,
                    policy=self.DESC_SYNC_RETRY_POLICY,
                )
            logger.warning(
                "Catalog movie desc fetch failed movie_id={} movie_number={} code={} retryable={}",
                movie.id,
                movie.movie_number,
                exc.error_code,
                exc.retryable,
            )
            return False
        with lock_context:
            self._apply_movie_desc(movie, movie_desc)
            ResourceTaskLedger.finish_success(attempt, record)
        return True

    def fetch_movie_desc_strict(self, movie: Movie) -> str:
        """只抓不记账：维护熔断计数，失败一律抛带 error_code 的 TaskItemError。"""
        try:
            movie_desc = self.dmm_provider.get_movie_desc(movie.movie_number)
        except DmmMovieNumberNotFoundError as exc:
            # 业务性失败说明 DMM 仍可用：清零熔断计数，判终态。
            self._dmm_request_failures = 0
            raise TaskItemError(
                "dmm_movie_number_not_found", str(exc), retryable=False
            ) from exc
        except DmmMovieDescNotFoundError as exc:
            self._dmm_request_failures = 0
            raise TaskItemError(
                "dmm_movie_desc_not_found", str(exc), retryable=False
            ) from exc
        except MetadataRequestError as exc:
            # 连通性失败（已重试耗尽）计入熔断。
            self._dmm_request_failures += 1
            if self._dmm_request_failures >= self.DMM_UNAVAILABLE_FAILURE_THRESHOLD:
                self._dmm_circuit_open = True
                logger.warning(
                    "DMM marked unavailable after {} consecutive request failures, "
                    "skip desc sync for the rest of this run",
                    self._dmm_request_failures,
                )
            raise TaskItemError("dmm_request_error", str(exc)) from exc
        except Exception as exc:
            self._dmm_request_failures = 0
            raise TaskItemError("dmm_fetch_failed", str(exc)) from exc
        self._dmm_request_failures = 0
        return movie_desc

    def ensure_dmm_available_or_abort(self) -> None:
        """cron runner 的逐资源前置检查：熔断已开则中止整轮（剩余资源不耗预算）。"""
        if self._dmm_circuit_open:
            raise TaskAbortError(
                "dmm_unavailable", "DMM 连续请求失败已熔断，本轮剩余影片中止"
            )

    @staticmethod
    def _apply_movie_desc(movie: Movie, movie_desc: str) -> None:
        movie.desc = movie_desc
        movie.save(only=[Movie.desc])

    def _refresh_movie_metadata_records_strict(
        self,
        *,
        movie: Movie,
        detail: JavdbMovieDetailResource,
        actors: list[JavdbMovieActorResource],
        tags: list,
        thin_cover_resolution: ThinCoverResolution,
        cover_task: ImagePersistTask | None,
        plot_tasks: list[ImagePersistTask],
        actor_image_tasks_by_javdb_id: dict[str, ImagePersistTask],
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

        images_to_cleanup: dict[int, Image] = {}
        for image in [old_cover_image, old_thin_cover_image, *[plot_link.image for plot_link in old_plot_links]]:
            if image is None:
                continue
            images_to_cleanup[int(image.id)] = image
        for image in images_to_cleanup.values():
            obsolete_paths.update(self.image_service.delete_image_record_if_unused(image))

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
        movie.cover_image = self.image_service.persist_refreshed_image_record(cover_task)
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

        plot_images_by_index: dict[int, Image] = {}
        # 剧照整批一次 upsert，避免逐张 get_or_none + create 的 2N 次往返。
        plot_images_by_path = self.image_service.persist_refreshed_image_records(plot_tasks)
        for plot_task in plot_tasks:
            plot_image = plot_images_by_path.get(plot_task.relative_path)
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
        profile_image_task: ImagePersistTask | None,
    ) -> tuple[Actor, set[str]]:
        actor = Actor.get_or_none(Actor.javdb_id == actor_resource.javdb_id)
        if actor is None:
            profile_image = self.image_service.persist_refreshed_image_record(profile_image_task)
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
            obsolete_paths.update(self.image_service.delete_image_record_if_unused(old_profile_image))

        profile_image = self.image_service.persist_refreshed_image_record(profile_image_task)
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

    def backfill_movie_thin_cover(self, movie: Movie) -> bool:
        """基于已落盘的封面和剧情图，为历史影片补算竖封面图。"""
        lock_context = self.persist_lock or nullcontext()
        obsolete_paths: set[str] = set()
        with lock_context, get_database().atomic():
            movie = Movie.get_by_id(movie.id)
            old_thin_cover_image = movie.thin_cover_image
            plot_links = list(
                MoviePlotImage.select(MoviePlotImage, Image)
                .join(Image)
                .where(MoviePlotImage.movie == movie)
                .order_by(MoviePlotImage.id)
            )
            thin_cover_resolution = self.image_service.resolve_thin_cover_from_existing_movie(movie, plot_links)
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
        self.image_service.delete_obsolete_image_files(obsolete_paths)
        refreshed_movie = Movie.get_by_id(movie.id)
        return refreshed_movie.thin_cover_image_id is not None

    def upsert_actor_from_javdb_resource(
        self,
        actor_resource: JavdbMovieActorResource,
        profile_image_task: ImagePersistTask | None = None,
    ) -> Actor:
        """把 JavDB 演员资源同步到本地，并在可用时补全头像。"""
        if profile_image_task is None:
            profile_image = self.image_service.persist_image(
                owner_type="actor",
                owner_key=actor_resource.javdb_id,
                image_url=actor_resource.avatar_url,
            )
        else:
            profile_image = self.image_service.persist_prepared_image(profile_image_task)

        lock_context = self.persist_lock or nullcontext()
        with lock_context, get_database().atomic():
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

    def _unique_preserve_order(self, items: list[str]) -> list[str]:
        """在保留 JavDB 原始顺序的前提下去重。"""
        unique_items: list[str] = []
        seen_items = set()
        for item in items:
            if item in seen_items:
                continue
            seen_items.add(item)
            unique_items.append(item)
        return unique_items
