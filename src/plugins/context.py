"""插件访问宿主能力的稳定门面。

具体 service/provider 在方法内懒导入，避免插件加载阶段反向依赖任务注册表。
插件只允许使用本类方法与 ``src.plugins.types`` 中的公开类型。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from src.plugins.types import (
    ACTOR_SNAPSHOT_FIELDS,
    MOVIE_SNAPSHOT_FIELDS,
    ActorPage,
    ActorSnapshot,
    MoviePage,
    MovieQueryFilters,
    MovieSnapshot,
    PluginCollection,
    PluginDownloadCandidate,
    PluginDownloadResult,
    PluginDownloadTarget,
    PluginMediaPresence,
    PluginMediaSnapshot,
    PluginNotification,
    PluginSubscription,
    PluginSubscriptionPage,
    PluginSubscriptionStatusCounts,
    SubtitleAsset,
    SubtitleContent,
    TagSnapshot,
)


class ActorApi:
    """context.actors：演员只读快照与资料 patch，不暴露 ORM。"""

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    @staticmethod
    def _to_snapshot(actor) -> ActorSnapshot:
        return ActorSnapshot(
            actor_id=actor.id,
            revision=actor.mutation_revision,
            values=MappingProxyType({name: getattr(actor, name) for name in ACTOR_SNAPSHOT_FIELDS}),
            owners=MappingProxyType(dict(actor.field_owners or {})),
        )

    def get(self, actor_id: int) -> ActorSnapshot | None:
        from src.model import Actor

        actor = Actor.get_or_none(Actor.id == actor_id)
        return self._to_snapshot(actor) if actor is not None else None

    def list_page(self, *, after_id: int = 0, limit: int = 500) -> ActorPage:
        if type(after_id) is not int or after_id < 0:
            raise ValueError("after_id 必须是非负整数")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        from src.model import Actor

        rows = list(Actor.select().where(Actor.id > after_id).order_by(Actor.id).limit(limit + 1))
        page = rows[:limit]
        return ActorPage(
            items=tuple(self._to_snapshot(actor) for actor in page),
            next_cursor=page[-1].id if len(rows) > limit else None,
        )

    def patch(self, actor_id: int, fields: dict[str, Any], expected_revision: int) -> bool:
        from src.service.catalog.actor_ownership_gateway import ActorOwnershipGateway

        return ActorOwnershipGateway.patch_plugin(actor_id, self._plugin_id, fields, expected_revision)


class MovieApi:
    """``context.movies``：影片只读快照与受保护字段 patch（v2-lite 契约 v2）。

    读取和 patch 都不暴露 ORM 对象；写入只经 MovieOwnershipGateway，插件拿不到
    任何可写句柄。字段 owner 与 revision 语义见 v2-lite 设计文档第 3/4 节。
    """

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    @classmethod
    def _to_snapshot(cls, movie) -> MovieSnapshot:
        return cls._to_snapshots([movie])[0]

    @staticmethod
    def _to_snapshots(movies) -> list[MovieSnapshot]:
        from src.model import Actor, MovieActor, MovieSeries, MovieTag, Tag

        if not movies:
            return []
        movie_ids = [movie.id for movie in movies]
        actors = {movie_id: [] for movie_id in movie_ids}
        tags = {movie_id: [] for movie_id in movie_ids}
        # 一页内批量读取关联，避免每部影片/每位演员单独查询。
        for link in (
            MovieActor.select(MovieActor, Actor).join(Actor)
            .where(MovieActor.movie.in_(movie_ids))
            .order_by(MovieActor.movie, MovieActor.actor)
        ):
            actors[link.movie_id].append(ActorApi._to_snapshot(link.actor))
        for link in (
            MovieTag.select(MovieTag, Tag).join(Tag)
            .where(MovieTag.movie.in_(movie_ids))
            .order_by(MovieTag.movie, MovieTag.tag)
        ):
            tags[link.movie_id].append(TagSnapshot(tag_id=link.tag_id, name=link.tag.name))
        series_ids = {movie.series_id for movie in movies if movie.series_id is not None}
        series_names = {
            series.id: series.name
            for series in MovieSeries.select().where(MovieSeries.id.in_(series_ids))
        } if series_ids else {}
        return [
            MovieSnapshot(
                movie_id=movie.id,
                revision=movie.mutation_revision,
                values=MappingProxyType({
                    name: series_names.get(movie.series_id) if name == "series_name"
                    else getattr(movie, name)
                    for name in MOVIE_SNAPSHOT_FIELDS
                }),
                owners=MappingProxyType(dict(movie.field_owners or {})),
                actors=tuple(actors[movie.id]),
                tags=tuple(tags[movie.id]),
            )
            for movie in movies
        ]

    def get(self, movie_id: int) -> MovieSnapshot | None:
        """按内部 id 读取影片快照；不存在返回 None。"""
        from src.model import Movie

        movie = Movie.get_or_none(Movie.id == movie_id)
        if movie is None:
            return None
        return self._to_snapshot(movie)

    def find_by_numbers(self, numbers) -> list[MovieSnapshot]:
        """按番号批量查找（大小写不敏感 + 分隔符候选，与人工输入点查同语义）。

        结果按输入顺序去重返回；找不到的番号跳过。
        """
        from src.common.service_helpers import find_movie_by_number

        movies = []
        seen_ids: set[int] = set()
        for number in numbers:
            movie = find_movie_by_number(number)
            if movie is None or movie.id in seen_ids:
                continue
            seen_ids.add(movie.id)
            movies.append(movie)
        return self._to_snapshots(movies)

    def list_page(self, *, after_id: int = 0, limit: int = 500) -> MoviePage:
        """按 Movie.id 游标分页遍历全库，返回不可变影片快照。"""
        if after_id < 0:
            raise ValueError("after_id 不能小于 0")
        if not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")

        from src.model import Movie

        rows = list(
            Movie.select()
            .where(Movie.id > after_id)
            .order_by(Movie.id)
            .limit(limit + 1)
        )
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        return MoviePage(
            items=tuple(self._to_snapshots(page_rows)),
            next_cursor=page_rows[-1].id if has_more else None,
        )

    @staticmethod
    def _coerce_query_filters(
        filters: MovieQueryFilters | Mapping[str, Any] | None,
    ) -> MovieQueryFilters:
        if filters is None:
            return MovieQueryFilters()
        if isinstance(filters, MovieQueryFilters):
            return filters
        if isinstance(filters, Mapping):
            values = dict(filters)
            tag_ids = values.get("tag_ids", ())
            values["tag_ids"] = () if tag_ids is None else tuple(tag_ids)
            return MovieQueryFilters(**values)
        raise TypeError("filters 必须是 MovieQueryFilters、Mapping 或 None")

    def query(
        self,
        filters: MovieQueryFilters | Mapping[str, Any] | None = None,
        *,
        after_id: int = 0,
        limit: int = 500,
    ) -> MoviePage:
        """按宿主影片筛选逻辑游标查询，返回不可变快照。"""
        if type(after_id) is not int or after_id < 0:
            raise ValueError("after_id 必须是非负整数")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")

        query_filters = self._coerce_query_filters(filters)
        from src.model import Movie
        from src.schema.catalog.movies import (
            MovieCollectionType,
            MovieListStatus,
            MovieNumberSource,
            TagMatchMode,
        )
        from src.service.catalog.movie_service import MovieService

        try:
            tag_match = TagMatchMode(query_filters.tag_match)
            status = MovieListStatus(query_filters.status)
            collection_type = MovieCollectionType(query_filters.collection_type)
            number_source = MovieNumberSource(query_filters.number_source)
        except ValueError as exc:
            raise ValueError(f"影片查询筛选值无效: {exc}") from exc

        if query_filters.status != "all" and (
            query_filters.subscribed is not None or query_filters.playable is not None
        ):
            raise ValueError("status 不能与 subscribed/playable 同时使用")
        if query_filters.subscribed is not None:
            if type(query_filters.subscribed) is not bool:
                raise ValueError("subscribed 必须是布尔值")
            status = (
                MovieListStatus.SUBSCRIBED
                if query_filters.subscribed
                else MovieListStatus.UNSUBSCRIBED
            )
        elif query_filters.playable is True:
            status = MovieListStatus.PLAYABLE
        if query_filters.playable is not None and type(query_filters.playable) is not bool:
            raise ValueError("playable 必须是布尔值")

        query = MovieService.movie_list_query(
            actor_id=query_filters.actor_id,
            tag_ids=list(query_filters.tag_ids) or None,
            tag_match=tag_match,
            year=query_filters.year,
            status=status,
            collection_type=collection_type,
            series_id=query_filters.series_id,
            director_name=query_filters.director_name,
            maker_name=query_filters.maker_name,
            number_source=number_source,
            heat_min=query_filters.heat_min,
            heat_max=query_filters.heat_max,
            blacklisted=query_filters.blacklisted,
        )
        search = (query_filters.search or "").strip()
        if search:
            keyword = f"%{search}%"
            query = query.where((Movie.movie_number ** keyword) | (Movie.title ** keyword))
        if query_filters.playable is not None and (
            query_filters.subscribed is not None or query_filters.playable is False
        ):
            playable_expression = MovieService._playable_exists_expression()
            query = query.where(
                playable_expression
                if query_filters.playable
                else ~playable_expression
            )

        # movie_list_query 默认按展示字段排序；插件查询使用 id 游标，重设为稳定的 id 顺序。
        rows = list(query.where(Movie.id > after_id).order_by(Movie.id).limit(limit + 1))
        page_rows = rows[:limit]
        return MoviePage(
            items=tuple(self._to_snapshots(page_rows)),
            next_cursor=page_rows[-1].id if len(rows) > limit else None,
        )

    def patch(
        self,
        movie_id: int,
        fields: dict[str, Any],
        expected_revision: int,
    ) -> bool:
        """写受保护字段（白名单内）并取得/续持 owner，乐观并发提交。

        不存在、revision 不匹配、字段由人工/其他插件持有，或试图屏蔽已订阅影片时，
        返回 False 且整次零修改；插件应重新读取 snapshot 后决定是否重试。
        """
        from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway

        return MovieOwnershipGateway.patch_plugin(
            movie_id,
            self._plugin_id,
            fields,
            expected_revision,
        )


class SubtitleApi:
    """``context.subtitles``：已登记影片字幕的只读访问。"""

    @staticmethod
    def list(movie_id: int) -> tuple[SubtitleAsset, ...]:
        """列出仍可访问的字幕；影片不存在时抛 SubtitleReadError。"""
        from src.service.catalog.movie_subtitle_service import MovieSubtitleService

        return MovieSubtitleService.list_subtitle_assets(movie_id)

    @staticmethod
    def read(movie_id: int, subtitle_id: int) -> SubtitleContent:
        """读取属于该影片的字幕，最多 10 MiB；失败抛 SubtitleReadError。"""
        from src.service.catalog.movie_subtitle_service import MovieSubtitleService

        return MovieSubtitleService.read_subtitle_content(movie_id, subtitle_id)


class MediaApi:
    """``context.media``：按影片读取只读媒体快照。"""

    @staticmethod
    def _validate_positive_id(value: int, field_name: str) -> None:
        if type(value) is not int or value <= 0:
            raise ValueError(f"{field_name} 必须为正整数")

    @staticmethod
    def _to_snapshot(
        media,
        *,
        movie_id: int,
        movie_number: str,
    ) -> PluginMediaSnapshot:
        raw_video_info = media.video_info
        video_info = (
            MappingProxyType(dict(raw_video_info))
            if isinstance(raw_video_info, Mapping)
            else None
        )
        return PluginMediaSnapshot(
            media_id=media.id,
            movie_id=movie_id,
            movie_number=movie_number,
            library_id=media.library_id,
            library_name=media.library.name,
            provider_key=media.library.provider_key,
            file_name=media.file_name,
            resolution=media.resolution,
            file_size_bytes=media.file_size_bytes,
            duration_seconds=media.duration_seconds,
            valid=media.valid,
            video_info=video_info,
        )

    def list_for_movie(
        self,
        movie_id: int,
        *,
        library_id: int | None = None,
    ) -> tuple[PluginMediaSnapshot, ...]:
        """读取一部影片的全部媒体，可按媒体库过滤。"""
        self._validate_positive_id(movie_id, "movie_id")
        if library_id is not None:
            self._validate_positive_id(library_id, "library_id")

        from src.model import Media, MediaLibrary, Movie

        movie = Movie.get_or_none(Movie.id == movie_id)
        if movie is None:
            return ()
        query = (
            Media.select(Media, MediaLibrary)
            .join(MediaLibrary)
            .where(Media.movie == movie.movie_number)
            .order_by(Media.id.asc())
        )
        if library_id is not None:
            query = query.where(Media.library == library_id)
        return tuple(
            self._to_snapshot(
                media,
                movie_id=movie.id,
                movie_number=movie.movie_number,
            )
            for media in query
        )

    def presence_for_movies(
        self,
        movie_ids: Collection[int],
        *,
        library_id: int | None = None,
    ) -> Mapping[int, PluginMediaPresence]:
        """批量读取影片媒体存在性，避免插件逐部查询造成 N+1。"""
        movie_ids = tuple(dict.fromkeys(movie_ids))
        for movie_id in movie_ids:
            self._validate_positive_id(movie_id, "movie_id")
        if library_id is not None:
            self._validate_positive_id(library_id, "library_id")
        if not movie_ids:
            return MappingProxyType({})

        from src.model import Media, MediaLibrary, Movie

        movies = list(
            Movie.select(Movie.id, Movie.movie_number)
            .where(Movie.id.in_(movie_ids))
        )
        movie_by_number = {movie.movie_number: movie for movie in movies}
        items_by_movie_id: dict[int, list[PluginMediaSnapshot]] = {
            movie.id: [] for movie in movies
        }
        if movie_by_number:
            query = (
                Media.select(Media, MediaLibrary)
                .join(MediaLibrary)
                .where(Media.movie.in_(tuple(movie_by_number)))
                .order_by(Media.id.asc())
            )
            if library_id is not None:
                query = query.where(Media.library == library_id)
            for media in query:
                movie = movie_by_number.get(media.movie_number)
                if movie is None:
                    continue
                items_by_movie_id[movie.id].append(
                    self._to_snapshot(
                        media,
                        movie_id=movie.id,
                        movie_number=movie.movie_number,
                    )
                )

        return MappingProxyType({
            movie_id: PluginMediaPresence(
                has_any=bool(items),
                has_playable=any(item.valid for item in items),
                items=tuple(items),
            )
            for movie_id, items in items_by_movie_id.items()
        })


class PluginDownloadService:
    """``context.downloads``：按目标下载器调用宿主搜索与提交链路。"""

    @staticmethod
    def _validate_positive_id(value: int, field_name: str) -> None:
        if type(value) is not int or value <= 0:
            raise ValueError(f"{field_name} 必须为正整数")

    @staticmethod
    def _to_target(client) -> PluginDownloadTarget:
        return PluginDownloadTarget(
            download_client_id=client.id,
            download_client_name=client.name,
            library_id=client.library_id,
            library_name=client.library.name,
            provider_key=client.library.provider_key,
        )

    @classmethod
    def _to_candidate(cls, raw_candidate, client) -> PluginDownloadCandidate:
        if raw_candidate.resolved_client_id != client.id:
            from src.api.exception.errors import ApiError

            raise ApiError(
                500,
                "plugin_download_candidate_target_mismatch",
                "宿主搜索候选的目标下载器不一致",
            )
        target = cls._to_target(client)
        return PluginDownloadCandidate(
            source_uri=raw_candidate.source_uri,
            indexer_name=raw_candidate.indexer_name,
            indexer_kind=raw_candidate.indexer_kind,
            download_client_id=target.download_client_id,
            download_client_name=target.download_client_name,
            library_id=target.library_id,
            library_name=target.library_name,
            provider_key=target.provider_key,
            movie_number=raw_candidate.movie_number,
            title=raw_candidate.title,
            size_bytes=raw_candidate.size_bytes,
            seeders=raw_candidate.seeders,
        )

    def get_target(self, download_client_id: int) -> PluginDownloadTarget:
        """读取下载器当前关联的媒体库目标。"""
        self._validate_positive_id(download_client_id, "download_client_id")
        from src.service.transfers.downloads.common import require_client

        return self._to_target(require_client(download_client_id))

    def search_candidates(
        self,
        *,
        movie_number: str,
        download_client_id: int,
        indexer_kind: str | None = None,
    ) -> tuple[PluginDownloadCandidate, ...]:
        """只搜索绑定到目标下载器的索引器，并固定候选投递目标。"""
        self._validate_positive_id(download_client_id, "download_client_id")
        from src.service.transfers.downloads.common import require_client
        from src.service.transfers.downloads.search_service import DownloadSearchService

        client = require_client(download_client_id)
        raw_candidates = DownloadSearchService().search_candidates(
            movie_number=movie_number,
            indexer_kind=indexer_kind,
            download_client_id=client.id,
        )
        return tuple(self._to_candidate(candidate, client) for candidate in raw_candidates)

    def submit(
        self,
        *,
        movie_number: str,
        candidate: PluginDownloadCandidate,
    ) -> PluginDownloadResult:
        """提交宿主生成的候选，并拒绝已变更的下载目标。"""
        if not isinstance(candidate, PluginDownloadCandidate):
            raise TypeError("candidate 必须是 PluginDownloadCandidate")

        from src.api.exception.errors import ApiError
        from src.service.transfers.downloads.common import (
            require_client,
            validate_non_empty,
        )

        normalized_movie_number = validate_non_empty(
            movie_number,
            "invalid_download_request_movie_number",
            "movie_number cannot be empty",
        ).upper()
        from src.common.movie_numbers import normalize_movie_number

        if normalize_movie_number(candidate.movie_number) != normalize_movie_number(movie_number):
            raise ApiError(
                422,
                "plugin_download_candidate_movie_mismatch",
                "下载候选与影片番号不匹配",
            )

        client = require_client(candidate.download_client_id)
        if (
            client.library_id != candidate.library_id
            or client.library.provider_key != candidate.provider_key
        ):
            raise ApiError(
                409,
                "plugin_download_candidate_stale",
                "下载候选的媒体库或提供方已发生变化",
            )

        from src.schema.transfers.downloads import DownloadRequestCreateRequest
        from src.service.transfers.downloads.request_service import (
            DownloadRequestService,
        )

        response = DownloadRequestService().create_request(
            DownloadRequestCreateRequest(
                client_id=client.id,
                movie_number=normalized_movie_number,
                candidate={
                    "source_uri": candidate.source_uri,
                    "indexer_name": candidate.indexer_name,
                    "title": candidate.title,
                    "size_bytes": candidate.size_bytes,
                    "seeders": candidate.seeders,
                },
            )
        )
        return PluginDownloadResult(task_id=response.task.id, created=response.created)


class SubscriptionApi:
    """``context.subscriptions``：订阅意图与宿主计算出的处理状态。"""

    @staticmethod
    def _to_snapshot(item) -> PluginSubscription:
        return PluginSubscription(
            movie_id=item.movie_id,
            movie_number=item.movie_number,
            title=item.title,
            status=item.status.value if hasattr(item.status, "value") else str(item.status),
            subscribed_at=item.subscribed_at,
            is_fresh=item.is_fresh,
            attempt_count=item.attempt_count,
            attempt_limit=item.attempt_limit,
            last_searched_at=item.last_searched_at,
            last_error=item.last_error,
            import_status=item.import_status,
            dead_download_task_count=item.dead_download_task_count,
            media_count=item.media_count,
        )

    def list(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        status: str = "all",
        sort: str = "subscribed_at:desc",
        search: str | None = None,
    ) -> PluginSubscriptionPage:
        from src.schema.catalog.subscriptions import (
            MovieSubscriptionSort,
            MovieSubscriptionStatus,
        )
        from src.service.catalog.movie_subscription_service import (
            MovieSubscriptionService,
        )

        try:
            parsed_status = MovieSubscriptionStatus(status)
            parsed_sort = MovieSubscriptionSort(sort)
        except ValueError as exc:
            raise ValueError(f"订阅筛选值无效: {exc}") from exc
        page_resource = MovieSubscriptionService.list_subscriptions(
            page=page,
            page_size=page_size,
            status=parsed_status,
            sort=parsed_sort,
            search=search,
        )
        return PluginSubscriptionPage(
            items=tuple(self._to_snapshot(item) for item in page_resource.items),
            page=page_resource.page,
            page_size=page_resource.page_size,
            total=page_resource.total,
        )

    def count_by_status(self) -> PluginSubscriptionStatusCounts:
        from src.service.catalog.movie_subscription_service import (
            MovieSubscriptionService,
        )

        resource = MovieSubscriptionService.count_by_status()
        return PluginSubscriptionStatusCounts(
            counts=MappingProxyType(resource.model_dump()),
        )

    def get(self, movie_id: int) -> PluginSubscription | None:
        if type(movie_id) is not int or movie_id <= 0:
            raise ValueError("movie_id 必须为正整数")
        from src.service.catalog.movie_subscription_service import (
            MovieSubscriptionService,
        )

        resource = MovieSubscriptionService.get_subscription(movie_id)
        return self._to_snapshot(resource) if resource is not None else None

    def subscribe(self, movie_number: str) -> None:
        from src.service.catalog.movie_service import MovieService

        MovieService.set_subscription(movie_number, True)

    def unsubscribe(self, movie_number: str) -> None:
        from src.service.catalog.movie_service import MovieService

        MovieService.unsubscribe_movie(movie_number)

    def reset_search(self, movie_ids: Collection[int] | None = None) -> int:
        from src.service.catalog.movie_subscription_search_state_service import (
            MovieSubscriptionSearchStateService,
        )

        if movie_ids is None:
            return MovieSubscriptionSearchStateService.reset()
        ids = tuple(dict.fromkeys(movie_ids))
        for movie_id in ids:
            if type(movie_id) is not int or movie_id <= 0:
                raise ValueError("movie_ids 必须是正整数")
        if not ids:
            return 0
        return MovieSubscriptionSearchStateService.reset(list(ids))


class NotificationApi:
    """``context.notifications``：创建插件通知并按插件隔离幂等键。"""

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    def _dedupe_key(self, key: str | None) -> str | None:
        if key is None:
            return None
        if not isinstance(key, str):
            raise TypeError("dedupe_key 必须是字符串")
        normalized = key.strip()
        if not normalized:
            raise ValueError("dedupe_key 不能为空")
        namespaced = f"plugin:{self._plugin_id}:{normalized}"
        if len(namespaced) > 255:
            raise ValueError("dedupe_key 过长")
        return namespaced

    def _to_snapshot(self, resource) -> PluginNotification:
        dedupe_key = resource.dedupe_key
        prefix = f"plugin:{self._plugin_id}:"
        if dedupe_key is not None and dedupe_key.startswith(prefix):
            dedupe_key = dedupe_key[len(prefix) :]
        return PluginNotification(
            notification_id=resource.id,
            category=resource.category,
            title=resource.title,
            content=resource.content,
            event_type=resource.event_type,
            dedupe_key=dedupe_key,
            resource_type=resource.resource_type,
            resource_id=resource.resource_id,
            created_at=resource.created_at,
            updated_at=resource.updated_at,
        )

    def _draft(
        self,
        *,
        category: str,
        title: str,
        content: str,
        event_type: str | None = None,
        dedupe_key: str | None = None,
        resource_type: str | None = None,
        resource_id: int | None = None,
        related_task_run_id: int | None = None,
        related_resource_type: str | None = None,
        related_resource_id: int | None = None,
    ):
        from src.service.system.activity.notifications import NotificationDraft

        return NotificationDraft(
            category=category,
            title=title,
            content=content,
            event_type=event_type,
            dedupe_key=self._dedupe_key(dedupe_key),
            resource_type=resource_type,
            resource_id=resource_id,
            related_task_run_id=related_task_run_id,
            related_resource_type=related_resource_type,
            related_resource_id=related_resource_id,
        )

    def create(
        self,
        *,
        category: str,
        title: str,
        content: str,
        event_type: str | None = None,
        dedupe_key: str | None = None,
        resource_type: str | None = None,
        resource_id: int | None = None,
        related_task_run_id: int | None = None,
        related_resource_type: str | None = None,
        related_resource_id: int | None = None,
    ) -> PluginNotification:
        from src.service.system.activity.notifications import NotificationService

        resource = NotificationService.create(
            self._draft(
                category=category,
                title=title,
                content=content,
                event_type=event_type,
                dedupe_key=dedupe_key,
                resource_type=resource_type,
                resource_id=resource_id,
                related_task_run_id=related_task_run_id,
                related_resource_type=related_resource_type,
                related_resource_id=related_resource_id,
            )
        )
        return self._to_snapshot(resource)

    def create_once(
        self,
        *,
        category: str,
        title: str,
        content: str,
        dedupe_key: str,
        event_type: str | None = None,
        resource_type: str | None = None,
        resource_id: int | None = None,
        related_task_run_id: int | None = None,
        related_resource_type: str | None = None,
        related_resource_id: int | None = None,
    ) -> PluginNotification:
        from src.service.system.activity.notifications import NotificationService

        resource = NotificationService.create_once(
            self._draft(
                category=category,
                title=title,
                content=content,
                event_type=event_type,
                dedupe_key=dedupe_key,
                resource_type=resource_type,
                resource_id=resource_id,
                related_task_run_id=related_task_run_id,
                related_resource_type=related_resource_type,
                related_resource_id=related_resource_id,
            )
        )
        return self._to_snapshot(resource)

    def resolve(self, dedupe_key: str) -> int:
        from src.service.system.activity.notifications import NotificationService

        namespaced_key = self._dedupe_key(dedupe_key)
        if namespaced_key is None:
            raise ValueError("dedupe_key 不能为空")
        return NotificationService.release_notification_dedupe_key(
            namespaced_key
        )


class CollectionApi:
    """``context.collections``：插件按 key 管理自己创建的三类合集。"""

    def __init__(self, plugin_id: str):
        self._plugin_id = plugin_id

    @staticmethod
    def _to_collection(collection_type: str, collection) -> PluginCollection:
        from src.model import MomentCollectionItem, PlaylistMovie

        if collection_type == "playlist":
            member_count = PlaylistMovie.select().where(
                PlaylistMovie.playlist == collection.id
            ).count()
        elif collection_type == "moment":
            member_count = MomentCollectionItem.select().where(
                MomentCollectionItem.collection == collection.id
            ).count()
        else:
            from src.service.collections.clip_collection_service import (
                ClipCollectionService,
            )

            member_count = ClipCollectionService._collection_counts([collection.id]).get(
                collection.id, 0
            )
        return PluginCollection(
            collection_type=collection_type,
            collection_id=collection.id,
            key=collection.plugin_key,
            name=collection.name,
            description=collection.description,
            member_count=member_count,
        )

    def ensure_playlist(
        self, key: str, name: str, description: str | None = None
    ) -> PluginCollection:
        from src.service.collections.plugin_collection_service import (
            PluginCollectionService,
        )

        return self._to_collection(
            "playlist",
            PluginCollectionService.ensure_playlist(
                self._plugin_id, key, name, description
            ),
        )

    def set_playlist_movies(self, key: str, movie_numbers) -> PluginCollection:
        from src.service.collections.plugin_collection_service import (
            PluginCollectionService,
        )

        return self._to_collection(
            "playlist",
            PluginCollectionService.set_playlist_movies(
                self._plugin_id, key, movie_numbers
            ),
        )

    def ensure_moment(
        self, key: str, name: str, description: str | None = None
    ) -> PluginCollection:
        from src.service.collections.plugin_collection_service import (
            PluginCollectionService,
        )

        return self._to_collection(
            "moment",
            PluginCollectionService.ensure_moment(
                self._plugin_id, key, name, description
            ),
        )

    def set_moment_points(self, key: str, point_ids) -> PluginCollection:
        from src.service.collections.plugin_collection_service import (
            PluginCollectionService,
        )

        return self._to_collection(
            "moment",
            PluginCollectionService.set_moment_points(
                self._plugin_id, key, point_ids
            ),
        )

    def ensure_clip(
        self, key: str, name: str, description: str | None = None
    ) -> PluginCollection:
        from src.service.collections.plugin_collection_service import (
            PluginCollectionService,
        )

        return self._to_collection(
            "clip",
            PluginCollectionService.ensure_clip(
                self._plugin_id, key, name, description
            ),
        )

    def set_clip_clips(self, key: str, clip_ids) -> PluginCollection:
        from src.service.collections.plugin_collection_service import (
            PluginCollectionService,
        )

        return self._to_collection(
            "clip",
            PluginCollectionService.set_clip_clips(
                self._plugin_id, key, clip_ids
            ),
        )


@dataclass(frozen=True, init=False)
class PluginContext:
    """插件上下文：配置只读、数据目录归插件所有、宿主能力按方法暴露。"""

    plugin_id: str
    settings: Mapping[str, Any]
    _data_dir: Path

    def __init__(self, plugin_id: str, settings: Mapping[str, Any], data_dir: Path):
        object.__setattr__(self, "plugin_id", plugin_id)
        object.__setattr__(self, "settings", settings)
        object.__setattr__(self, "_data_dir", Path(data_dir))

    def ensure_data_dir(self) -> Path:
        """确保插件数据目录存在并返回（``<root>/<plugin_id>/data``）。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    @property
    def data_dir(self) -> Path:
        return self.ensure_data_dir()

    @property
    def actors(self) -> ActorApi:
        """演员资料读写；身份及订阅字段只读。"""
        return ActorApi(self.plugin_id)

    @property
    def movies(self) -> MovieApi:
        """影片只读快照与受保护字段写入出口（v2-lite 契约 v2）。"""
        return MovieApi(self.plugin_id)

    @property
    def media(self) -> MediaApi:
        """媒体只读快照与按媒体库的存在性查询。"""
        return MediaApi()

    @property
    def downloads(self) -> PluginDownloadService:
        """按目标下载器搜索并提交下载候选。"""
        return PluginDownloadService()

    @property
    def subscriptions(self) -> SubscriptionApi:
        """订阅影片的状态查询与受控状态操作。"""
        return SubscriptionApi()

    @property
    def notifications(self) -> NotificationApi:
        """创建用户可见通知；dedupe key 自动带插件命名空间。"""
        return NotificationApi(self.plugin_id)

    @property
    def collections(self) -> CollectionApi:
        """管理插件自己按 key 创建的影片、时刻和片段合集。"""
        return CollectionApi(self.plugin_id)

    @property
    def subtitles(self) -> SubtitleApi:
        """字幕元信息、原始字节和内容指纹；不提供可写句柄。"""
        return SubtitleApi()

    @staticmethod
    def build_javdb_provider(
        username: str | None = None,
        password: str | None = None,
    ):
        """构建 JavDB provider；账号仅需登录的榜单（TOP250）需要，由插件从自身设置传入。"""
        from src.metadata.factory import build_javdb_provider

        return build_javdb_provider(username=username, password=password)

    @staticmethod
    def build_catalog_import_service():
        """构造目录导入服务。"""
        from src.service.catalog import CatalogImportService

        return CatalogImportService()

    def import_movie_by_number(
        self,
        movie_number: str,
        *,
        force_subscribed: bool = False,
    ) -> MovieSnapshot:
        """按 JavDB 优先规则导入，未找到时调用元数据来源插件；本地已存在则复用。

        返回不可变 MovieSnapshot（不暴露 ORM 对象）；插件要更新既有字段，必须
        重新取得 snapshot 并单独调用 ``context.movies.patch``。
        批量任务应分别构造并复用 provider/importer，避免每个番号重复创建客户端。
        """
        provider = self.build_javdb_provider()
        importer = self.build_catalog_import_service()
        from src.service.catalog.metadata_source_service import MetadataSourceService

        movie, _created = MetadataSourceService.import_by_number(
            movie_number, provider, importer,
            force_subscribed=force_subscribed,
        )
        return MovieApi._to_snapshot(movie)

    def list_existing_movie_numbers(self) -> set[str]:
        """主库全部影片番号的大写集合，供插件做 O(1) 存在性判定。"""
        from src.model import Movie

        return {
            (row[0] or "").upper()
            for row in Movie.select(Movie.movie_number).tuples()
        }

    def import_subtitle(
        self,
        movie_number: str,
        content: bytes,
        filename: str,
        language: str | None = None,
    ):
        """给影片写入一段字幕内容；统一处理扩展名校验、去重、落盘与登记。"""
        from src.service.catalog.subtitle_asset_service import SubtitleAssetService

        return SubtitleAssetService.import_subtitle_content(
            movie_number,
            content,
            filename,
            language=language,
        )

    def sync_ranking_sources(
        self,
        progress_callback=None,
    ) -> dict[str, int]:
        """同步当前插件声明的全部排行榜来源，返回统计 dict。"""
        from src.service.discovery.ranking_service import (
            RANKING_SOURCE_OWNERS,
            RankingSyncService,
        )

        source_keys = tuple(
            source_key
            for source_key, owner in RANKING_SOURCE_OWNERS.items()
            if owner == self.plugin_id
        )
        if not source_keys:
            raise RuntimeError(f"插件 {self.plugin_id} 未注册排行榜来源")
        return RankingSyncService().sync_all_rankings(
            progress_callback=progress_callback,
            source_keys=source_keys,
        )

    def sync_ranking_board(
        self,
        source_key: str,
        board_key: str,
        period: str | None = None,
    ) -> dict[str, int | str]:
        """同步单个榜单；source_key 必须是本插件声明的来源。"""
        from src.service.discovery.ranking_service import (
            RANKING_SOURCE_OWNERS,
            RankingSyncService,
        )

        if RANKING_SOURCE_OWNERS.get(source_key) != self.plugin_id:
            raise ValueError(
                f"排行榜来源 {source_key} 不属于插件 {self.plugin_id}"
            )
        return RankingSyncService().sync_board_period(
            source_key=source_key,
            board_key=board_key,
            period=period,
        )

    @staticmethod
    def get_task_logger(name: str):
        from src.scheduler.logging import get_task_logger

        return get_task_logger(name)
