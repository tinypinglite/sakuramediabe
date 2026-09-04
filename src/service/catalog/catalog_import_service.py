"""目录导入 service。

负责把 JavDB 返回的影片/演员详情转换成本地目录数据。图片下载与图片记录持久化已抽到
``MovieImageService``，本 service 只做元数据编排，图片相关能力统一委托 ``self.image_service``。
阅读入口建议从导入语义的三个方法开始：
``import_movie_if_missing``（纯新建）、``refresh_movie_metadata_strict``（纯覆盖）、
``update_movie_fields``（指定字段更新）。
"""

from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from loguru import logger

from src.common.runtime_time import utc_now_for_db
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
from src.model.catalog.movies import PROTECTED_MOVIE_FIELDS
from src.service.catalog.movie_heat_service import MovieHeatService
from src.service.catalog.movie_image_service import (
    ImageDownloadError,
    ImagePersistTask,
    MovieImageService,
    PreparedImageFile,
    ThinCoverResolution,
)
from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway

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

    def __init__(
        self,
        image_downloader: Callable[[str, Path], None] | None = None,
        persist_lock=None,
    ):
        # 图片子系统统一交由 MovieImageService，downloader 透传下去保住 media_import 的注入接缝。
        self.image_service = MovieImageService(image_downloader=image_downloader)
        self.persist_lock = persist_lock

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

    def import_movie_if_missing(
        self,
        detail: JavdbMovieDetailResource,
        force_subscribed: bool = False,
    ) -> tuple[Movie, bool]:
        """纯新建语义：影片已存在（movie_number 或 javdb_id 命中）时跳过，不写任何字段。

        返回 ``(movie, created)``。已存在影片的元数据刷新唯一入口是
        ``refresh_movie_metadata_strict``（手动刷新接口）；指定字段更新走
        ``update_movie_fields``。
        """
        # 快路径：已存在直接返回，零图片 IO。图片下载期间可能被并发导入抢先建好，
        # 事务内会二次确认（图片路径按番号确定性派生，双方下载内容一致，无孤儿文件问题）。
        existing_movie = Movie.get_or_none(
            (Movie.movie_number == detail.movie_number)
            | (Movie.javdb_id == detail.javdb_id)
        )
        if existing_movie is not None:
            logger.debug(
                "Catalog import skipped existing movie movie_id={} movie_number={}",
                existing_movie.id,
                existing_movie.movie_number,
            )
            return existing_movie, False

        actors = detail.actors or []
        tags = detail.tags or []
        plot_images = detail.plot_images or []
        logger.info(
            "Catalog import start movie_number={} javdb_id={} actors={} tags={} plot_images={}",
            detail.movie_number,
            detail.javdb_id,
            len(actors),
            len(tags),
            len(plot_images),
        )
        plot_urls = self._unique_preserve_order(plot_images)
        if len(plot_urls) != len(plot_images):
            logger.debug(
                "Catalog import deduplicated plot images movie_number={} original={} deduplicated={}",
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
            # 二次确认：图片下载期间并发导入可能已建好该影片，此时跳过写入。
            movie = Movie.get_or_none(
                (Movie.movie_number == detail.movie_number)
                | (Movie.javdb_id == detail.javdb_id)
            )
            if movie is not None:
                logger.debug(
                    "Catalog import concurrent created movie movie_id={} movie_number={}",
                    movie.id,
                    movie.movie_number,
                )
                return movie, False
            movie = Movie(
                movie_number=detail.movie_number,
                javdb_id=detail.javdb_id,
                title=detail.title,
            )
            # 纯新建路径：无旧封面/订阅状态可继承，直接按详情写入。
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
                    movie.subscribed_at = utc_now_for_db()
                else:
                    movie.subscribed_at = None
            movie.extra = detail.extra
            movie.title = detail.title
            movie.javdb_id = detail.javdb_id
            movie.movie_number = detail.movie_number
            movie.save()
            logger.debug(
                "Catalog import movie saved movie_id={} movie_number={}",
                movie.id,
                movie.movie_number,
            )

            # 演员、标签、剧照关系都使用 get_or_create，避免多次导入产生重复关联。
            for actor_resource in actors:
                actor = self.upsert_actor_from_javdb_resource(
                    actor_resource,
                    profile_image_task=actor_image_tasks_by_javdb_id.get(actor_resource.javdb_id),
                    update_gender=True,
                )
                MovieActor.get_or_create(movie=movie, actor=actor)
                logger.debug(
                    "Catalog import actor linked movie_id={} actor_id={} actor_javdb_id={}",
                    movie.id,
                    actor.id,
                    actor.javdb_id,
                )

            for tag_resource in tags:
                tag, _ = Tag.get_or_create(name=tag_resource.name)
                MovieTag.get_or_create(movie=movie, tag=tag)
                logger.debug(
                    "Catalog import tag linked movie_id={} tag_id={} tag_name={}",
                    movie.id,
                    tag.id,
                    tag.name,
                )

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
                        "Catalog import plot image linked movie_id={} image_id={} index={}",
                        movie.id,
                        plot_image.id,
                        plot_task.plot_index,
                    )
            obsolete_paths.update(
                self._apply_thin_cover_resolution(
                    movie,
                    None,
                    thin_cover_resolution,
                    plot_images_by_index,
                    refreshed=False,
                )
            )

        self.image_service.delete_obsolete_image_files(obsolete_paths)
        MovieHeatService.update_single_movie_heat(movie.id)
        logger.info(
            "Catalog import finished movie_id={} movie_number={}",
            movie.id,
            movie.movie_number,
        )
        return movie, True

    # ③ 允许更新的字段白名单 -> detail 取值器；heat 是推导列不允许直接写，
    # 图片/演员/标签/剧照等关联字段不在本机制内（新建时由 import_movie_if_missing 完整导入）。
    _MOVIE_FIELD_UPDATE_MAP: dict[str, Callable[[JavdbMovieDetailResource], Any]] = {
        "score": lambda detail: detail.score or 0,
        "score_number": lambda detail: detail.score_number,
        "watched_count": lambda detail: detail.watched_count,
        "want_watch_count": lambda detail: detail.want_watch_count,
        "comment_count": lambda detail: detail.comment_count,
        "title": lambda detail: detail.title,
        "summary": lambda detail: detail.summary,
        "maker_name": lambda detail: detail.maker_name,
        "director_name": lambda detail: detail.director_name,
    }

    def update_movie_fields(
        self,
        detail: JavdbMovieDetailResource,
        fields: tuple[str, ...],
    ) -> tuple[Movie, bool, tuple[str, ...]]:
        """指定字段更新：影片不存在先完整导入（import_movie_if_missing），存在则只更新指定字段。

        返回 ``(movie, created, updated_fields)``，updated_fields 是实际发生变化的字段
        （变更检测：值一致的字段跳过不写）；heat 联动由调用方负责（本方法不感知热度公式）。
        """
        if not fields:
            raise ValueError("fields 不能为空")
        normalized_fields = tuple(dict.fromkeys(fields))
        invalid_fields = sorted(set(normalized_fields) - set(self._MOVIE_FIELD_UPDATE_MAP))
        if invalid_fields:
            raise ValueError(f"不支持的字段: {', '.join(invalid_fields)}")
        movie, created = self.import_movie_if_missing(detail)
        # 变更检测：只写值真正变化的字段，避免无意义 UPDATE，也支撑调用方的 updated/unchanged 计数。
        previous_values: dict[str, Any] = {}
        changed_fields: list[str] = []
        for field_name in normalized_fields:
            target_value = self._MOVIE_FIELD_UPDATE_MAP[field_name](detail)
            previous_value = getattr(movie, field_name)
            if previous_value == target_value:
                continue
            previous_values[field_name] = previous_value
            setattr(movie, field_name, target_value)
            changed_fields.append(field_name)
        if changed_fields:
            # 受保护字段（白名单内，如 title/summary）分流到唯一写入网关，宿主侧
            # 只更新未接管字段；其余字段保持窄更新。已持久化对象不能裸 save（护栏）。
            protected_changed = [
                field_name
                for field_name in changed_fields
                if field_name in PROTECTED_MOVIE_FIELDS
            ]
            host_changed = [
                field_name
                for field_name in changed_fields
                if field_name not in PROTECTED_MOVIE_FIELDS
            ]
            if host_changed:
                movie.save(
                    only=[Movie._meta.fields[field_name] for field_name in host_changed]
                )
            if protected_changed:
                MovieOwnershipGateway.update_host_unowned(
                    movie.id,
                    {
                        field_name: getattr(movie, field_name)
                        for field_name in protected_changed
                    },
                )
                # 被插件接管的字段未落库：重读该行并把实际落库变更回流到结果，
                # 保证返回对象与库内真实状态一致、updated 字段不虚报。
                movie = Movie.get_by_id(movie.id)
                changed_fields = [
                    field_name
                    for field_name in changed_fields
                    if getattr(movie, field_name) != previous_values[field_name]
                ]
        return movie, created, tuple(changed_fields)

    def refresh_movie_metadata_strict(
        self,
        movie: Movie,
        detail: JavdbMovieDetailResource,
    ) -> Movie:
        """纯覆盖语义：按远端详情全量覆盖已存在影片的元数据（含 title/summary/互动数/图片/演员/标签），不触碰订阅与番号字段。

        是已存在影片元数据刷新的唯一入口（手动刷新接口），影片必须已存在。
        """
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
        old_plot_image_ids: list[int] = []
        try:
            lock_context = self.persist_lock or nullcontext()
            with lock_context, get_database().atomic():
                persisted_movie, obsolete_paths, old_plot_image_ids = self._refresh_movie_metadata_records_strict(
                    movie=movie,
                    detail=detail,
                    actors=actors,
                    tags=tags,
                    thin_cover_resolution=thin_cover_resolution,
                    cover_task=cover_task,
                    plot_tasks=plot_tasks,
                    actor_image_tasks_by_javdb_id=actor_image_tasks_by_javdb_id,
                )
            if old_plot_image_ids:
                try:
                    from src.service.discovery.qdrant_plot_image_store import (
                        get_qdrant_plot_image_store,
                    )

                    get_qdrant_plot_image_store().delete_by_plot_image_ids(old_plot_image_ids)
                except Exception as exc:
                    logger.warning(
                        "Delete refreshed plot image vectors failed count={} detail={}",
                        len(old_plot_image_ids),
                        exc,
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
    ) -> tuple[Movie, set[str], list[int]]:
        # 事务内先锁行重读（v2-lite 字段主权）：锁定期间当前 owner 状态稳定，
        # 之后受保护字段写入以本次读取为准，杜绝旧快照覆盖插件刚写入的值。
        movie = (
            Movie.select()
            .where(Movie.id == movie.id)
            .for_update()
            .get()
        )
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
        old_plot_image_ids = [link.id for link in old_plot_links]

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

        # 受保护字段（白名单内）不允许随宿主窄更新落库，改走唯一写入网关
        # （update_host_unowned 只更新未接管字段）；其余字段显式窄更新。
        host_field_names = (
            "release_date",
            "duration_minutes",
            "score",
            "score_number",
            "watched_count",
            "want_watch_count",
            "comment_count",
            "summary",
            "series",
            "maker_name",
            "director_name",
            "extra",
            "javdb_id",
            "title",
            "cover_image",
        )
        protected_field_names = set(PROTECTED_MOVIE_FIELDS) & set(host_field_names)
        narrow_columns = [
            Movie._meta.fields[name]
            for name in host_field_names
            if name not in protected_field_names
        ]
        movie.save(only=narrow_columns)
        if protected_field_names:
            MovieOwnershipGateway.update_host_unowned(
                movie.id,
                {name: getattr(movie, name) for name in protected_field_names},
            )

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

        # 受保护字段可能未全部落库（被插件接管的字段保留插件值）：重读该行，
        # 保证返回对象与库内真实状态一致（内存中的远端值不得外泄给调用方）。
        movie = Movie.get_by_id(movie.id)
        return movie, obsolete_paths, old_plot_image_ids

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
        actor.profile_image = profile_image
        actor.save(only=[Actor.name, Actor.alias_name, Actor.javdb_type, Actor.profile_image])
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
        *,
        update_gender: bool = False,
    ) -> Actor:
        """把 JavDB 演员资源同步到本地，并在可用时补全头像。

        演员搜索等非影片入库场景只能同步身份和头像信息；只有新影片入库
        显式传入 ``update_gender=True`` 时，才使用影片详情中的性别写入本地。
        """
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
            defaults = {
                "name": actor_resource.name,
                "alias_name": merged_alias_name,
                "profile_image": profile_image,
                "javdb_type": actor_resource.javdb_type,
            }
            if update_gender:
                defaults["gender"] = actor_resource.gender
            actor, created = Actor.get_or_create(
                javdb_id=actor_resource.javdb_id,
                defaults=defaults,
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
                if update_gender:
                    actor.gender = actor_resource.gender
                if profile_image is not None:
                    actor.profile_image = profile_image
                columns = [Actor.name, Actor.alias_name, Actor.javdb_type]
                if update_gender:
                    columns.append(Actor.gender)
                if profile_image is not None:
                    columns.append(Actor.profile_image)
                actor.save(only=columns)

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
