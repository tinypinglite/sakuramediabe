import os
import time
from datetime import date, datetime, timedelta

from peewee import fn

from src.common.media_import_status import (
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_SKIPPED,
    UNFINISHED_IMPORT_STATUSES,
)
from src.common.runtime_time import (
    get_runtime_timezone,
    runtime_now,
    to_db_utc_naive,
    to_runtime_local_naive,
    utc_now_for_db,
)
from src.config.config import settings
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import (
    SYSTEM_PLAYLIST_KINDS,
    Actor,
    BackgroundTaskRun,
    ClipCollection,
    ClipCollectionItem,
    DownloadTask,
    Media,
    MediaLibrary,
    MediaProgress,
    MediaThumbnail,
    MomentCollection,
    MomentCollectionItem,
    Movie,
    Playlist,
    PlaylistMovie,
    VideoCollection,
    VideoCollectionItem,
)
from src.schema.system.status import (
    StatusActorSummary,
    StatusCollectionsSummary,
    StatusCollectionSummary,
    StatusDownloadTaskSummary,
    StatusEmbeddingServiceSummary,
    StatusImageSearchIndexingSummary,
    StatusImageSearchIndexSpaceSummary,
    StatusImageSearchResource,
    StatusImageSearchVectorStoreSummary,
    StatusInsightsResource,
    StatusMediaFileSummary,
    StatusMediaLibrarySummary,
    StatusMediaLibraryUsage,
    StatusMetadataProviderTestError,
    StatusMetadataProviderTestResource,
    StatusMovieSummary,
    StatusResource,
    StatusThumbnailSummary,
    StatusWatchTrendBucket,
    StatusWatchTrendGranularity,
    StatusWatchTrendRange,
    StatusWatchTrendResource,
)
from src.service.discovery.embedding_client import (
    EmbeddingClientError,
    get_embedding_client,
)
from src.service.discovery.image_search_index_space_service import (
    ImageSearchIndexSpaceService,
)
from src.service.discovery.qdrant_thumbnail_store import (
    QdrantThumbnailStore,
    get_qdrant_thumbnail_store,
)
from src.service.playback.media_library_service import MediaLibraryService
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.system.optional_services import image_search_enabled


class StatusService:
    FEMALE_GENDER = 1
    BACKEND_VERSION_ENV_KEY = "SAKURAMEDIA_BACKEND_VERSION"
    BACKEND_VERSION_DEFAULT = "dev-local"
    METADATA_PROVIDER_TEST_MOVIE_NUMBER = "SSNI-888"
    # 下载任务六分类字段名，作为分桶结果的唯一真相源。
    DOWNLOAD_TASK_BUCKETS = (
        "downloading",
        "importing",
        "imported",
        "import_failed",
        "skipped",
        "download_failed",
    )
    # 观看趋势按天分桶的范围与桶数（含今天）；其余范围（1y/all）按月。
    WATCH_TREND_DAY_COUNTS = {
        StatusWatchTrendRange.LAST_7_DAYS: 7,
        StatusWatchTrendRange.LAST_30_DAYS: 30,
        StatusWatchTrendRange.LAST_90_DAYS: 90,
    }
    WATCH_TREND_LAST_YEAR_MONTHS = 12

    @classmethod
    def get_status(cls) -> StatusResource:
        female_total = Actor.select().where(Actor.gender == cls.FEMALE_GENDER).count()
        female_subscribed = (
            Actor.select()
            .where((Actor.gender == cls.FEMALE_GENDER) & (Actor.is_subscribed == True))
            .count()
        )

        movie_total = Movie.select().count()
        movie_subscribed = Movie.select().where(Movie.is_subscribed == True).count()
        movie_playable = (
            Media.select(fn.COUNT(fn.DISTINCT(Media.movie)))
            .where(Media.valid == True)
            .scalar()
            or 0
        )

        media_file_total = Media.select().count()
        media_file_total_size_bytes = (
            Media.select(fn.COALESCE(fn.SUM(Media.file_size_bytes), 0)).scalar() or 0
        )

        media_library_total = MediaLibrary.select().count()

        # 待生成缩略图的媒体文件数复用缩略图服务的判定口径；缩略图文件数即 MediaThumbnail 行数（与 Media 一对多）。
        pending_thumbnail_media = MediaThumbnailService.count_pending_media()
        retry_wait_thumbnail_media = MediaThumbnailService.count_retry_wait_media()
        terminal_thumbnail_media = MediaThumbnailService.count_terminal_failed_media()
        thumbnail_total = MediaThumbnail.select().count()

        return StatusResource(
            backend_version=cls._resolve_backend_version(),
            actors=StatusActorSummary(
                female_total=int(female_total),
                female_subscribed=int(female_subscribed),
            ),
            movies=StatusMovieSummary(
                total=int(movie_total),
                subscribed=int(movie_subscribed),
                playable=int(movie_playable),
            ),
            media_files=StatusMediaFileSummary(
                total=int(media_file_total),
                total_size_bytes=int(media_file_total_size_bytes),
            ),
            media_libraries=StatusMediaLibrarySummary(total=int(media_library_total)),
            thumbnails=StatusThumbnailSummary(
                pending_media=int(pending_thumbnail_media),
                retry_wait_media=int(retry_wait_thumbnail_media),
                terminal_failed_media=int(terminal_thumbnail_media),
                total=int(thumbnail_total),
            ),
        )

    @classmethod
    def get_insights(cls) -> StatusInsightsResource:
        return StatusInsightsResource(
            download_tasks=cls._download_task_summary(),
            media_libraries=cls._media_library_usages(),
            collections=cls._collection_summaries(),
        )

    @classmethod
    def get_watch_trend(cls, range_value: StatusWatchTrendRange) -> StatusWatchTrendResource:
        """按最后观看时间聚合观看分布；详见 StatusWatchTrendResource 的口径说明。"""
        runtime_timezone = get_runtime_timezone()
        today = runtime_now().date()
        granularity = cls._watch_trend_granularity(range_value)
        start_local_date = cls._watch_trend_start_date(range_value, today)
        if range_value is StatusWatchTrendRange.ALL:
            start_local_date = cls._earliest_watched_local_date()

        bucket_movies: dict[str, set[str]] = {}
        if start_local_date is not None:
            window_start_utc = cls._local_day_start_utc(start_local_date, runtime_timezone)
            # 上界取本地明天零点且不包含：半开区间，避免窗口外的脏时间戳进入计数。
            window_end_utc = cls._local_day_start_utc(
                today + timedelta(days=1), runtime_timezone
            )
            rows = (
                MediaProgress.select(MediaProgress.last_watched_at, Media.movie)
                .join(Media)
                .where(
                    cls._watched_progress_condition()
                    & Media.movie.is_null(False)
                    & (MediaProgress.last_watched_at >= window_start_utc)
                    & (MediaProgress.last_watched_at < window_end_utc)
                )
                .tuples()
            )
            for watched_at, movie_number in rows:
                period = cls._watch_trend_period(
                    to_runtime_local_naive(watched_at), granularity
                )
                bucket_movies.setdefault(period, set()).add(movie_number)

        buckets = [
            StatusWatchTrendBucket(
                period=period, count=len(bucket_movies.get(period, ()))
            )
            for period in cls._watch_trend_periods(start_local_date, today, granularity)
        ]
        return StatusWatchTrendResource(
            range=range_value,
            granularity=granularity,
            watched_movie_count=len(
                {number for numbers in bucket_movies.values() for number in numbers}
            ),
            buckets=buckets,
        )

    @staticmethod
    def _watched_progress_condition():
        # position_seconds > 0 即视为看过；last_watched_at 可空属历史形态，聚合前排除。
        return (MediaProgress.position_seconds > 0) & MediaProgress.last_watched_at.is_null(False)

    @staticmethod
    def _local_day_start_utc(local_date: date, runtime_timezone) -> datetime:
        # 用 datetime.min.time() 取当日 00:00，避免与模块级 time 模块同名。
        return to_db_utc_naive(
            datetime.combine(local_date, datetime.min.time()), assume_tz=runtime_timezone
        )

    @staticmethod
    def _earliest_watched_local_date() -> date | None:
        earliest = (
            MediaProgress.select(fn.MIN(MediaProgress.last_watched_at))
            .where(StatusService._watched_progress_condition())
            .scalar()
        )
        if earliest is None:
            return None
        return to_runtime_local_naive(earliest).date()

    @classmethod
    def _watch_trend_granularity(
        cls, range_value: StatusWatchTrendRange
    ) -> StatusWatchTrendGranularity:
        if range_value in cls.WATCH_TREND_DAY_COUNTS:
            return StatusWatchTrendGranularity.DAY
        return StatusWatchTrendGranularity.MONTH

    @classmethod
    def _watch_trend_start_date(
        cls, range_value: StatusWatchTrendRange, today: date
    ) -> date | None:
        day_count = cls.WATCH_TREND_DAY_COUNTS.get(range_value)
        if day_count is not None:
            return today - timedelta(days=day_count - 1)
        if range_value is StatusWatchTrendRange.LAST_YEAR:
            return cls._shift_month(
                date(today.year, today.month, 1), -(cls.WATCH_TREND_LAST_YEAR_MONTHS - 1)
            )
        # ALL 的起点由最早观看记录决定，无记录时返回空桶列表。
        return None

    @classmethod
    def _watch_trend_periods(
        cls,
        start_local_date: date | None,
        today: date,
        granularity: StatusWatchTrendGranularity,
    ) -> list[str]:
        if start_local_date is None:
            return []
        if granularity is StatusWatchTrendGranularity.DAY:
            periods = []
            cursor = start_local_date
            while cursor <= today:
                periods.append(cursor.isoformat())
                cursor += timedelta(days=1)
            return periods
        last_month = date(today.year, today.month, 1)
        periods = []
        cursor = date(start_local_date.year, start_local_date.month, 1)
        while cursor <= last_month:
            periods.append(cls._watch_trend_period(cursor, granularity))
            cursor = cls._shift_month(cursor, 1)
        return periods

    @staticmethod
    def _watch_trend_period(
        local_time: date | datetime, granularity: StatusWatchTrendGranularity
    ) -> str:
        if granularity is StatusWatchTrendGranularity.DAY:
            return local_time.date().isoformat()
        return f"{local_time.year:04d}-{local_time.month:02d}"

    @staticmethod
    def _shift_month(source: date, months: int) -> date:
        month_index = source.year * 12 + (source.month - 1) + months
        return date(month_index // 12, month_index % 12 + 1, 1)

    @classmethod
    def _download_task_summary(cls) -> StatusDownloadTaskSummary:
        rows = (
            DownloadTask.select(
                DownloadTask.state, DownloadTask.import_status, fn.COUNT(DownloadTask.id)
            )
            .group_by(DownloadTask.state, DownloadTask.import_status)
            .tuples()
        )
        counts = {bucket: 0 for bucket in cls.DOWNLOAD_TASK_BUCKETS}
        for state, import_status, total in rows:
            counts[cls._download_task_bucket(state, import_status)] += int(total or 0)
        return StatusDownloadTaskSummary(total=sum(counts.values()), **counts)

    @staticmethod
    def _download_task_bucket(state: str, import_status: str) -> str:
        """把 state × import_status 折叠成用户视角分类。

        未知取值统一向非终态桶靠（下载中 / 导入异常），保证总数不漏。
        """
        if state == "completed":
            if import_status == IMPORT_STATUS_COMPLETED:
                return "imported"
            if import_status == IMPORT_STATUS_SKIPPED:
                return "skipped"
            if import_status in UNFINISHED_IMPORT_STATUSES:
                return "importing"
            return "import_failed"
        if state == "failed":
            return "download_failed"
        return "downloading"

    @staticmethod
    def _media_library_usages() -> list[StatusMediaLibraryUsage]:
        usage_rows = (
            Media.select(
                Media.library,
                fn.COUNT(Media.id),
                fn.COALESCE(fn.SUM(Media.file_size_bytes), 0),
            )
            .group_by(Media.library)
            .tuples()
        )
        usage_by_library_id = {
            int(library_id): (int(file_count or 0), int(total_size_bytes or 0))
            for library_id, file_count, total_size_bytes in usage_rows
        }
        # 以媒体库表为基准，保证没有媒体的空库也出现在结果里。
        space_by_library_id = MediaLibraryService.storage_space_usages()
        usages = []
        for library in MediaLibrary.select().order_by(MediaLibrary.id.asc()):
            file_count, total_size_bytes = usage_by_library_id.get(library.id, (0, 0))
            space = space_by_library_id.get(library.id)
            usages.append(
                StatusMediaLibraryUsage(
                    library_id=library.id,
                    name=library.name,
                    provider_key=library.provider_key,
                    file_count=file_count,
                    total_size_bytes=total_size_bytes,
                    space_total_bytes=None if space is None else space.total_bytes,
                    space_used_bytes=None if space is None else space.used_bytes,
                    space_free_bytes=None if space is None else space.free_bytes,
                )
            )
        return usages

    @staticmethod
    def _collection_summaries() -> StatusCollectionsSummary:
        system_playlist_kinds = tuple(SYSTEM_PLAYLIST_KINDS)
        return StatusCollectionsSummary(
            playlists=StatusCollectionSummary(
                count=Playlist.select()
                .where(Playlist.kind.not_in(system_playlist_kinds))
                .count(),
                item_count=(
                    PlaylistMovie.select()
                    .join(Playlist)
                    .where(Playlist.kind.not_in(system_playlist_kinds))
                    .count()
                ),
            ),
            video_collections=StatusCollectionSummary(
                count=VideoCollection.select().count(),
                item_count=VideoCollectionItem.select().count(),
            ),
            clip_collections=StatusCollectionSummary(
                count=ClipCollection.select().count(),
                item_count=ClipCollectionItem.select().count(),
            ),
            moment_collections=StatusCollectionSummary(
                count=MomentCollection.select().count(),
                item_count=MomentCollectionItem.select().count(),
            ),
        )

    @classmethod
    def _resolve_backend_version(cls) -> str:
        # 后端版本由镜像构建阶段注入，未注入时回退本地开发默认值。
        backend_version = os.getenv(cls.BACKEND_VERSION_ENV_KEY)
        if backend_version:
            return backend_version
        return cls.BACKEND_VERSION_DEFAULT

    @classmethod
    def get_image_search_status(cls) -> StatusImageSearchResource:
        if not image_search_enabled():
            return StatusImageSearchResource(
                enabled=False,
                healthy=False,
                checked_at=utc_now_for_db(),
                embedding_service=StatusEmbeddingServiceSummary(healthy=False),
                image_search_vector_store=StatusImageSearchVectorStoreSummary(
                    healthy=False, url=settings.qdrant.url,
                    collection_name=QdrantThumbnailStore.COLLECTION_NAME, exists=False,
                ),
                indexing=StatusImageSearchIndexingSummary(pending_thumbnails=0, failed_thumbnails=0),
                index_space=StatusImageSearchIndexSpaceSummary(state="unavailable"),
            )
        embedding_service = cls._probe_embedding_service()
        image_search_vector_store = cls._probe_image_search_vector_store()
        indexing = cls._indexing_status()
        index_space = ImageSearchIndexSpaceService.get_status(
            embedding_service.space_id if embedding_service.healthy else None
        )
        return StatusImageSearchResource(
            enabled=True,
            healthy=bool(embedding_service.healthy and image_search_vector_store.healthy),
            checked_at=utc_now_for_db(),
            embedding_service=embedding_service,
            image_search_vector_store=image_search_vector_store,
            indexing=indexing,
            index_space=StatusImageSearchIndexSpaceSummary(
                state=index_space.state,
                indexed_space_id=index_space.indexed_space_id,
                current_space_id=index_space.current_space_id,
                is_rebuilding=cls._is_image_search_rebuilding(),
            ),
        )

    @classmethod
    def test_metadata_provider(cls, provider: str) -> StatusMetadataProviderTestResource:
        normalized_provider = provider.strip().lower()
        start_at = time.time()
        try:
            if normalized_provider == "javdb":
                return cls._test_javdb_provider(start_at=start_at)
            raise ValueError(f"unsupported metadata provider: {provider}")
        except MetadataNotFoundError as exc:
            return cls._build_metadata_provider_failure(
                provider=normalized_provider,
                start_at=start_at,
                error=StatusMetadataProviderTestError(
                    type="metadata_not_found",
                    message=str(exc),
                    resource=exc.resource,
                    lookup_value=exc.lookup_value,
                ),
            )
        except MetadataRequestError as exc:
            return cls._build_metadata_provider_failure(
                provider=normalized_provider,
                start_at=start_at,
                error=StatusMetadataProviderTestError(
                    type="metadata_request_error",
                    message=str(exc),
                    method=exc.method,
                    url=exc.url,
                ),
            )
        except Exception as exc:
            return cls._build_metadata_provider_failure(
                provider=normalized_provider,
                start_at=start_at,
                error=StatusMetadataProviderTestError(
                    type="unexpected_error",
                    message=str(exc),
                ),
            )

    @classmethod
    def _test_javdb_provider(cls, *, start_at: float) -> StatusMetadataProviderTestResource:
        # JavDB 联通性以真实按番号搜索并拉取详情为准；JavDB 请求永远直连（不叠 metadata proxy）。
        detail = build_javdb_provider().get_movie_by_number(
            cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER
        )
        return StatusMetadataProviderTestResource(
            healthy=True,
            checked_at=utc_now_for_db(),
            provider="javdb",
            movie_number=cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER,
            elapsed_ms=cls._elapsed_ms(start_at),
            javdb_id=detail.javdb_id,
            title=detail.title,
            actors_count=len(detail.actors),
            tags_count=len(detail.tags),
        )

    @classmethod
    def _build_metadata_provider_failure(
        cls,
        *,
        provider: str,
        start_at: float,
        error: StatusMetadataProviderTestError,
    ) -> StatusMetadataProviderTestResource:
        return StatusMetadataProviderTestResource(
            healthy=False,
            checked_at=utc_now_for_db(),
            provider=provider,
            movie_number=cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER,
            elapsed_ms=cls._elapsed_ms(start_at),
            error=error,
        )

    @staticmethod
    def _elapsed_ms(start_at: float) -> int:
        return int((time.time() - start_at) * 1000)

    @classmethod
    def _probe_embedding_service(cls) -> StatusEmbeddingServiceSummary:
        try:
            space = get_embedding_client().describe()
        except EmbeddingClientError as exc:
            return StatusEmbeddingServiceSummary(
                healthy=False,
                endpoint=str(settings.image_search.inference_base_url),
                error=exc.message,
            )
        except Exception as exc:
            return StatusEmbeddingServiceSummary(
                healthy=False,
                endpoint=str(settings.image_search.inference_base_url),
                error=str(exc),
            )
        return StatusEmbeddingServiceSummary(
            healthy=True,
            endpoint=str(settings.image_search.inference_base_url),
            space_id=space.space_id,
            dimension=space.dimension,
            modalities=sorted(space.modalities),
        )

    @staticmethod
    def _probe_image_search_vector_store() -> StatusImageSearchVectorStoreSummary:
        try:
            store = get_qdrant_thumbnail_store()
            status = store.inspect_status()
            return StatusImageSearchVectorStoreSummary(
                healthy=bool(status.get("healthy", False)),
                url=str(status.get("url", getattr(store, "url", ""))),
                collection_name=str(status.get("collection_name", getattr(store, "collection_name", ""))),
                exists=bool(status.get("exists", False)),
                points_count=(int(status["points_count"]) if status.get("points_count") is not None else None),
                vector_size=(int(status["vector_size"]) if status.get("vector_size") is not None else None),
                vector_dtype=(str(status["vector_dtype"]) if status.get("vector_dtype") is not None else None),
                collection_status=(
                    str(status["collection_status"]) if status.get("collection_status") is not None else None
                ),
                error=(str(status["error"]) if status.get("error") else None),
            )
        except Exception as exc:
            return StatusImageSearchVectorStoreSummary(
                healthy=False,
                url=str(settings.qdrant.url),
                collection_name=QdrantThumbnailStore.COLLECTION_NAME,
                exists=False,
                error=str(exc),
            )

    @staticmethod
    def _indexing_status() -> StatusImageSearchIndexingSummary:
        pending = (
            MediaThumbnail.select()
            .where(MediaThumbnail.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING)
            .count()
        )
        failed = (
            MediaThumbnail.select()
            .where(MediaThumbnail.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_FAILED)
            .count()
        )
        return StatusImageSearchIndexingSummary(
            pending_thumbnails=int(pending),
            failed_thumbnails=int(failed),
        )

    @staticmethod
    def _is_image_search_rebuilding() -> bool:
        task_run = (
            BackgroundTaskRun.select(BackgroundTaskRun.params)
            .where(
                BackgroundTaskRun.task_key == "image_search_index",
                BackgroundTaskRun.state.in_(("pending", "running")),
            )
            .first()
        )
        return task_run is not None and (task_run.params or {}).get("reset") is True
