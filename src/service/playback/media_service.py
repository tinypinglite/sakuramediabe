import time
from collections.abc import Sequence
from pathlib import Path

import peewee
from loguru import logger

from src.api.exception.errors import ApiError
from src.common import (
    build_signed_cloud115_merged_hls_url,
    build_signed_media_url,
    build_signed_merged_media_url,
)
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    paginate,
    require_by_id,
    require_record,
    resolve_sort,
    resolve_sort_expression,
    unlink_ignore_missing,
    validate_page,
    with_movie_card_relations,
)
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    ResourceTaskState,
    VideoItem,
)
from src.model.base import get_database
from src.schema.catalog.actors import ImageResource
from src.schema.common.pagination import PageResponse
from src.schema.playback.media import (
    InvalidMediaResource,
    MediaListItemResource,
    MediaPlayUrlKind,
    MediaPlayUrlMode,
    MediaPlayUrlResource,
    MediaPlayUrlSegmentResource,
    MediaPlayUrlSource,
    MediaPointCreateRequest,
    MediaPointKind,
    MediaPointListItemResource,
    MediaPointResource,
    MediaProgressResource,
    MediaProgressUpdateRequest,
    MediaRapidUploadFilterStatus,
    MediaThumbnailResource,
    MediaValidityCheckResponse,
)
from src.service.catalog.image_cleanup_service import ImageCleanupService

# 直接从子模块导入：collections/__init__ 会引入 clip_collection_service -> playback 形成循环，
# 绕开包级 __init__ 的初始化顺序依赖。
from src.service.collections.playlist_service import PlaylistService
from src.service.discovery import get_qdrant_thumbnail_store
from src.service.playback.media_file_scan_service import MediaFileScanService
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.transfers.rapid_upload.query_service import (
    MediaRapidUploadQueryService,
)


class MediaService:
    MEDIA_POINT_SORT_FIELDS = {
        "created_at:desc": [MediaPoint.created_at.desc(), MediaPoint.id.desc()],
        "created_at:asc": [MediaPoint.created_at.asc(), MediaPoint.id.asc()],
    }

    MEDIA_LIST_SORT_FIELD_MAP = {
        "file_size_bytes": Media.file_size_bytes,
        "heat": Movie.heat,
    }
    # 非 JAV 视频没有关联 Movie，heat 恒为空，需要排在末尾（不受排序方向影响）。
    MEDIA_LIST_NULLABLE_SORT_FIELDS = {"heat"}

    @staticmethod
    @staticmethod
    def is_cloud115_media(media: Media) -> bool:
        # backend 判定的权威来源是所属库（Media 不冗余 backend 字段）。
        from src.model.enums import MediaLibraryBackend

        library = media.library
        return library is not None and library.backend == MediaLibraryBackend.CLOUD115.value

    @staticmethod
    def resolve_movie_play_url(
        movie_number: str | None,
        movie_id: int | None,
        source: MediaPlayUrlSource,
        mode: MediaPlayUrlMode,
    ) -> MediaPlayUrlResource:
        """解析影片播放链接：按播放源（本地/115）与播放模式（单个/合并）返回签名地址。

        合并顺序按 ``Media.id`` 升序（与详情页媒体列表一致）；本地多分段返回虚拟合并
        URL（真实规格校验在合并流端点进行，本方法只负责链接解析）；115 多资源合并返回
        后端 HLS 全量代理的合播 m3u8 地址。

        只选取 ``valid`` 媒体，本地候选额外要求 ``path`` 非空——library 被 SET NULL
        的云端孤儿行 path 恒空，不能当成本地源给一个必然 404 的链接。
        """
        if movie_number:
            movie = Movie.get_or_none(Movie.movie_number == movie_number)
        elif movie_id is not None:
            movie = Movie.get_or_none(Movie.id == movie_id)
        else:
            raise ApiError(422, "invalid_movie_filter", "需要 movie_number 或 movie_id")
        if movie is None:
            raise ApiError(404, "movie_not_found", "影片不存在")

        # 联表取 library，避免 is_cloud115_media 逐条懒加载造成 N+1。
        medias = list(
            Media.select(Media, MediaLibrary)
            .join(MediaLibrary, peewee.JOIN.LEFT_OUTER)
            .where(Media.movie == movie.movie_number, Media.valid == True)
            .order_by(Media.id)
        )
        local = [m for m in medias if not MediaService.is_cloud115_media(m) and m.path]
        cloud115 = [m for m in medias if MediaService.is_cloud115_media(m)]

        def _segments(items: list[Media]) -> list[MediaPlayUrlSegmentResource]:
            return [
                MediaPlayUrlSegmentResource(
                    media_id=item.id,
                    duration_seconds=item.duration_seconds or 0,
                )
                for item in items
            ]

        is_local = source == MediaPlayUrlSource.LOCAL
        candidates = local if is_local else cloud115
        if not candidates:
            return MediaPlayUrlResource(kind=MediaPlayUrlKind.NONE)

        if not is_local and mode == MediaPlayUrlMode.MERGED:
            media_ids = [item.id for item in candidates]
            return MediaPlayUrlResource(
                play_url=build_signed_cloud115_merged_hls_url(media_ids),
                kind=MediaPlayUrlKind.CLOUD115_MERGED,
                segment_count=len(candidates),
                segments=_segments(candidates),
            )

        if mode == MediaPlayUrlMode.MERGED and len(candidates) >= 2:
            media_ids = [item.id for item in candidates]
            return MediaPlayUrlResource(
                play_url=build_signed_merged_media_url(media_ids),
                kind=MediaPlayUrlKind.MERGED_LOCAL,
                segment_count=len(candidates),
                segments=_segments(candidates),
            )

        first = candidates[0]
        return MediaPlayUrlResource(
            play_url=build_signed_media_url(first.id),
            kind=(
                MediaPlayUrlKind.SINGLE_LOCAL
                if is_local
                else MediaPlayUrlKind.SINGLE_CLOUD115
            ),
            segment_count=1,
            segments=_segments(candidates[:1]),
        )

    # 115 直链进程内缓存：键 (media_id, signature, user_agent)
    # signature 由 /stream 签名 URL 提供，变了说明前端换了会话；UA 变要重取因为 115
    # 用 f= 指纹绑 UA，共享会 403。TTL 6h 远小于直链 t= 实测寿命，留足播放余量。
    _CLOUD115_URL_TTL_SECONDS = 6 * 60 * 60
    _cloud115_url_cache: dict[tuple[int, str, str], tuple[str, float]] = {}

    # 这些错误只表示 HLS 当前不可用，不应阻断仍可通过原画直链完成的播放。
    _CLOUD115_HLS_FALLBACK_CODES = {
        "cloud115_membership_required",
        "cloud115_video_transcoding",
        "cloud115_rate_limited",
        "cloud115_upstream_error",
        "cloud115_hls_unavailable",
        "hls_not_video",
    }

    @classmethod
    async def resolve_cloud115_playback_url(
        cls, media: Media, user_agent: str, signature: str
    ) -> str:
        """优先返回最高码率 HLS；可恢复的 HLS 错误静默降级到原画直链。"""
        from src.service.playback.cloud115_hls_service import Cloud115HlsService

        try:
            return await Cloud115HlsService.resolve_highest_variant_url(
                media,
                user_agent=user_agent,
            )
        except ApiError as exc:
            if exc.code not in cls._CLOUD115_HLS_FALLBACK_CODES:
                # cookies 失效、媒体不存在/被封等确定性错误必须原样暴露。
                raise
            logger.info(
                "cloud115 hls unavailable, falling back to direct stream "
                "media_id={} code={} detail={}",
                media.id,
                exc.code,
                exc.message,
            )
            return await cls.resolve_cloud115_stream_url(media, user_agent, signature)

    @classmethod
    async def resolve_cloud115_stream_url(
        cls, media: Media, user_agent: str, signature: str
    ) -> str:
        """按 (media_id, signature, user_agent) 复用直链；未命中才调 115 downurl。

        UA 绑定链路：播放器请求 /stream 的 UA → 绑进直链 f= 指纹 → 302 后播放器
        跟随请求 CDN 时 UA 天然一致；缓存命中直接复用该链。

       
        """
        from src.lib.cloud115 import Cloud115Error
        from src.service.cloud115 import (
            cloud115_client_for,
            map_cloud115_error,
        )

        locator = media.backend_locator or {}
        pickcode = locator.get("pickcode")
        if not pickcode:
            raise ApiError(
                404, "media_locator_missing",
                "媒体缺少 cloud115 定位信息",
                {"media_id": media.id},
            )

        cache_key = (media.id, signature, user_agent)
        now = time.monotonic()
        cached = cls._cloud115_url_cache.get(cache_key)
        if cached is not None:
            url, expires_at = cached
            if expires_at > now:
                logger.info(
                    "cloud115 stream url cache hit media_id={} pickcode={}",
                    media.id, pickcode,
                )
                return url
            cls._cloud115_url_cache.pop(cache_key, None)

        # 惰性清理已过期项，避免 dict 长期运行下无限膨胀（signature 每 12h 换一批）。
        for stale_key in [k for k, (_, exp) in cls._cloud115_url_cache.items() if exp <= now]:
            cls._cloud115_url_cache.pop(stale_key, None)

        try:
            async with cloud115_client_for(media.library) as client:
                direct = await client.get_download_url(pickcode, user_agent)
        except Cloud115Error as exc:
            logger.warning(
                "cloud115 stream url refetch failed media_id={} pickcode={} detail={}",
                media.id, pickcode, exc,
            )
            raise map_cloud115_error(exc) from exc

        cls._cloud115_url_cache[cache_key] = (
            direct.url,
            now + cls._CLOUD115_URL_TTL_SECONDS,
        )
        # 记录签名 UA 与新链地址，便于线上出问题时对比播放器实际访问 CDN 的报文。
        logger.info(
            "cloud115 stream url refetched media_id={} pickcode={} ua={!r} url={}",
            media.id, pickcode, user_agent, direct.url,
        )
        return direct.url

    @staticmethod
    def _require_media(media_id: int) -> Media:
        return require_by_id(Media, media_id, "media", error_message="Media not found")

    @staticmethod
    def _require_media_point_for_media(media_id: int, point_id: int) -> MediaPoint:
        return require_record(
            MediaPoint, MediaPoint.id == point_id, MediaPoint.media == media_id,
            error_code="media_point_not_found",
            error_message="Media point not found",
            error_details={"media_id": media_id, "point_id": point_id},
        )

    @staticmethod
    def _to_media_point_resource(point: MediaPoint) -> MediaPointResource:
        return MediaPointResource(
            point_id=point.id,
            media_id=point.media_id,
            thumbnail_id=point.thumbnail_id,
            offset_seconds=point.offset_seconds,
            image=ImageResource.from_attributes_model(point.thumbnail.image),
            created_at=point.created_at,
        )

    @staticmethod
    def _point_query_with_thumbnail():
        return (
            MediaPoint.select(MediaPoint, MediaThumbnail, Image)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
        )

    @staticmethod
    def _require_thumbnail_for_media(media: Media, thumbnail_id: int) -> MediaThumbnail:
        return require_record(
            MediaThumbnail,
            MediaThumbnail.id == thumbnail_id,
            MediaThumbnail.media == media,
            error_code="media_thumbnail_not_found",
            error_message="Media thumbnail not found",
            error_details={"media_id": media.id, "thumbnail_id": thumbnail_id},
            query=MediaThumbnail.select(MediaThumbnail),
        )

    @classmethod
    def _resolve_media_point_sort(cls, value: str | None) -> Sequence:
        return resolve_sort(
            value, cls.MEDIA_POINT_SORT_FIELDS,
            default_key="created_at:desc", error_code="invalid_media_point_filter",
        )

    @staticmethod
    def _validate_media_point_page(page: int, page_size: int) -> None:
        validate_page(page, page_size, error_code="invalid_media_point_filter")

    @staticmethod
    def _delete_local_media_file(media: Media) -> None:
        unlink_ignore_missing(Path(media.path))

    @classmethod
    def _delete_cloud115_media_file(cls, media: Media) -> None:
        """删 115 云端文件（进回收站，有误删缓冲）；文件已不在时容忍继续删记录。

        cookies 失效 / 限流等上游异常向上抛（映射成 ApiError），不静默吞——
        否则记录删了云端文件还在，库目录会积累孤儿文件。
        """
        from src.lib.cloud115 import Cloud115Error, Cloud115NotFoundError
        from src.service.cloud115 import (
            cloud115_client_for,
            map_cloud115_error,
        )

        locator = media.backend_locator or {}
        fid = locator.get("fid")
        if not fid:
            # 没有 fid 无从删起：记录本身仍应可删（对齐本地 FileNotFoundError 容忍语义）
            logger.warning("Delete cloud115 media without fid media_id={}", media.id)
            return

        async def _delete() -> None:
            async with cloud115_client_for(media.library) as client:
                await client.delete_files([fid])

        import asyncio

        try:
            asyncio.run(_delete())
        except Cloud115NotFoundError:
            logger.info(
                "Cloud115 file already gone media_id={} fid={}", media.id, fid
            )
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

    @staticmethod
    def _media_point_kind_filter(kind: MediaPointKind):
        # 按归属过滤：JAV 取有番号媒体，VIDEO 取非 JAV 视频媒体，ALL 不限制。
        if kind == MediaPointKind.JAV:
            return Media.movie.is_null(False)
        if kind == MediaPointKind.VIDEO:
            return Media.video_item.is_null(False)
        return None

    @classmethod
    def _build_media_list_sort(cls, sort: str | None) -> Sequence:
        """解析 ``field:direction`` 排序表达式，默认按入库时间倒序。"""
        return resolve_sort_expression(
            sort,
            cls.MEDIA_LIST_SORT_FIELD_MAP,
            error_code="invalid_media_filter",
            nullable_fields=cls.MEDIA_LIST_NULLABLE_SORT_FIELDS,
            tie_breaker=Media.id,
            default=[Media.created_at.desc(), Media.id.desc()],
        )

    @staticmethod
    def _to_media_list_item_resource(
        media: Media,
        *,
        last_rapid_upload_status: str | None = None,
    ) -> MediaListItemResource:
        # 按归属拆分：JAV 媒体展示番号、影片封面与热度，非 JAV 媒体回退到 VideoItem 标题。
        if media.movie_number:
            movie = media.movie
            return MediaListItemResource(
                id=media.id,
                kind="jav",
                movie_number=movie.movie_number,
                title=movie.title,
                cover_image=ImageResource.from_attributes_model(movie.cover_image)
                if movie.cover_image_id is not None
                else None,
                thin_cover_image=ImageResource.from_attributes_model(movie.thin_cover_image)
                if movie.thin_cover_image_id is not None
                else None,
                library_id=media.library_id,
                library_name=media.library.name if media.library_id is not None else None,
                path=media.display_path,
                file_size_bytes=media.file_size_bytes,
                duration_seconds=media.duration_seconds,
                resolution=media.resolution,
                special_tags=media.special_tags,
                valid=media.valid,
                heat=movie.heat,
                last_rapid_upload_status=last_rapid_upload_status,
                created_at=media.created_at,
                updated_at=media.updated_at,
            )
        video_item = media.video_item if media.video_item_id else None
        return MediaListItemResource(
            id=media.id,
            kind="video",
            video_item_id=media.video_item_id,
            title=video_item.title if video_item is not None else None,
            cover_image=ImageResource.from_attributes_model(video_item.cover_image)
            if video_item is not None and video_item.cover_image_id is not None
            else None,
            library_id=media.library_id,
            library_name=media.library.name if media.library_id is not None else None,
            path=media.display_path,
            file_size_bytes=media.file_size_bytes,
            duration_seconds=media.duration_seconds,
            resolution=media.resolution,
            special_tags=media.special_tags,
            valid=media.valid,
            heat=None,
            last_rapid_upload_status=last_rapid_upload_status,
            created_at=media.created_at,
            updated_at=media.updated_at,
        )

    @classmethod
    def list_media(
        cls,
        *,
        kind: MediaPointKind = MediaPointKind.ALL,
        library_id: int | None = None,
        actor_ids: list[int] | None = None,
        rapid_upload_status: MediaRapidUploadFilterStatus | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[MediaListItemResource]:
        """跨 JAV 与 videos 域分页列出全部媒体，支持归属/库/订阅女优/上次秒传状态筛选与排序。"""
        validate_page(page, page_size, error_code="invalid_media_filter")

        # 非 JAV 媒体没有 movie，Movie 改为 LEFT OUTER，并补 VideoItem 兜底标题/封面。
        base_query = Media.select(Media, Movie, VideoItem).join(
            Movie,
            peewee.JOIN.LEFT_OUTER,
            on=(Media.movie == Movie.movie_number),
        )
        base_query, _thin_cover_alias = with_movie_card_relations(base_query)
        video_cover_alias = Image.alias()
        base_query = (
            base_query.select_extend(MediaLibrary, video_cover_alias)
            .switch(Media)
            .join(VideoItem, peewee.JOIN.LEFT_OUTER)
            .join(
                video_cover_alias,
                peewee.JOIN.LEFT_OUTER,
                on=(VideoItem.cover_image == video_cover_alias.id),
                attr="cover_image",
            )
            .switch(Media)
            .join(MediaLibrary, peewee.JOIN.LEFT_OUTER)
        )

        kind_filter = cls._media_point_kind_filter(kind)
        if kind_filter is not None:
            base_query = base_query.where(kind_filter)
        if library_id is not None:
            base_query = base_query.where(Media.library == library_id)
        if actor_ids is not None:
            # MovieActor.movie 指向 Movie.id，而 Media.movie 指向 Movie.movie_number，
            # 两者不是同一标识符空间，须先转换成 movie_number 再筛 Media；
            # 用 IN 子查询而非 JOIN，避免多女优命中同一影片时主查询出现重复行，
            # 非 JAV 视频因 Media.movie 恒为 NULL 天然被排除，无需额外联动 kind。
            actor_movie_ids = MovieActor.select(MovieActor.movie).where(
                MovieActor.actor.in_(actor_ids)
            )
            movie_numbers = Movie.select(Movie.movie_number).where(
                Movie.id.in_(actor_movie_ids)
            )
            base_query = base_query.where(Media.movie.in_(movie_numbers))
        if rapid_upload_status is not None:
            if rapid_upload_status == MediaRapidUploadFilterStatus.NONE:
                # NONE 反选：排除"最新非 retried item 存在且非 succeeded"的 media，
                # 剩下的就是"从未秒传过 or 最近一次已成功切云端"。
                base_query = base_query.where(
                    Media.id.not_in(
                        MediaRapidUploadQueryService.active_media_id_subquery()
                    )
                )
            else:
                base_query = base_query.where(
                    Media.id.in_(
                        MediaRapidUploadQueryService.media_id_subquery_for_status(
                            rapid_upload_status.value
                        )
                    )
                )

        total = base_query.count()
        order_by = cls._build_media_list_sort(sort)
        rows = list(
            base_query.order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        # 一次子查询批量拿本页所有 media 的最新秒传状态，避免逐条 N+1。
        rapid_upload_status = MediaRapidUploadQueryService.get_latest_status_by_media(
            [media.id for media in rows]
        )
        items = [
            cls._to_media_list_item_resource(
                media,
                last_rapid_upload_status=rapid_upload_status.get(media.id),
            )
            for media in rows
        ]
        return PageResponse[MediaListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def list_media_points(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        sort: str | None = None,
        kind: MediaPointKind = MediaPointKind.JAV,
    ) -> PageResponse[MediaPointListItemResource]:
        cls._validate_media_point_page(page, page_size)
        start = (page - 1) * page_size
        order_by = cls._resolve_media_point_sort(sort)
        kind_filter = cls._media_point_kind_filter(kind)
        # total 与分页查询套用同一 kind 过滤，需 join Media 才能按归属筛。
        total_query = MediaPoint.select().join(Media)
        if kind_filter is not None:
            total_query = total_query.where(kind_filter)
        total = total_query.count()
        points_query = (
            MediaPoint.select(MediaPoint, Media, Movie, MediaThumbnail, Image)
            .join(Media)
            .switch(Media)
            # 非 JAV 媒体没有 movie，改为 LEFT OUTER JOIN 让两类时刻都能列出。
            .join(Movie, peewee.JOIN.LEFT_OUTER, on=(Media.movie == Movie.movie_number))
            .switch(MediaPoint)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
        )
        if kind_filter is not None:
            points_query = points_query.where(kind_filter)
        points = list(
            points_query
            .order_by(*order_by)
            .offset(start)
            .limit(page_size)
        )
        items = [
            MediaPointListItemResource(
                point_id=point.id,
                media_id=point.media_id,
                movie_number=point.media.movie_number,
                video_item_id=point.media.video_item_id,
                thumbnail_id=point.thumbnail_id,
                offset_seconds=point.offset_seconds,
                image=ImageResource.from_attributes_model(point.thumbnail.image),
                created_at=point.created_at,
            )
            for point in points
        ]
        return PageResponse[MediaPointListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def list_points(cls, media_id: int) -> list[MediaPointResource]:
        cls._require_media(media_id)
        points = (
            cls._point_query_with_thumbnail()
            .where(MediaPoint.media == media_id)
            .order_by(MediaPoint.id)
        )
        return [cls._to_media_point_resource(point) for point in points]

    @classmethod
    def create_point(
        cls,
        media_id: int,
        payload: MediaPointCreateRequest,
    ) -> tuple[MediaPointResource, bool]:
        media = cls._require_media(media_id)
        thumbnail = cls._require_thumbnail_for_media(media, payload.thumbnail_id)
        with get_database().atomic():
            point = (
                MediaPoint.select()
                .where(
                    MediaPoint.media == media,
                    MediaPoint.thumbnail == thumbnail,
                )
                .order_by(MediaPoint.id)
                .first()
            )
            if point is not None:
                point = cls._point_query_with_thumbnail().where(MediaPoint.id == point.id).get()
                return cls._to_media_point_resource(point), False

            point = MediaPoint.create(
                media=media,
                thumbnail=thumbnail,
                offset_seconds=thumbnail.offset,
            )
            point = cls._point_query_with_thumbnail().where(MediaPoint.id == point.id).get()
        return cls._to_media_point_resource(point), True

    @classmethod
    def delete_point(cls, media_id: int, point_id: int) -> None:
        cls._require_media(media_id)
        point = cls._require_media_point_for_media(media_id, point_id)
        point.delete_instance()

    @classmethod
    def update_progress(
        cls,
        media_id: int,
        payload: MediaProgressUpdateRequest,
    ) -> MediaProgressResource:
        media = cls._require_media(media_id)
        watched_at = utc_now_for_db()
        progress = MediaProgress.get_or_none(MediaProgress.media == media)
        if progress is None:
            progress = MediaProgress.create(
                media=media,
                position_seconds=payload.position_seconds,
                last_watched_at=watched_at,
                created_at=watched_at,
                updated_at=watched_at,
            )
        else:
            progress.position_seconds = payload.position_seconds
            progress.last_watched_at = watched_at
            progress.updated_at = watched_at
            progress.save()

        # 最近播放列表是 JAV 影片维度的能力，非 JAV 媒体跳过维护。
        if media.movie_number:
            PlaylistService.touch_recently_played(media.movie)
        return MediaProgressResource(
            media_id=media.id,
            last_position_seconds=progress.position_seconds,
            last_watched_at=progress.last_watched_at,
        )

    @classmethod
    def delete_media(cls, media_id: int) -> None:
        media = cls._require_media(media_id)
        # 秒传运行期间源文件正在被哈希或清理，禁止并发删除同一媒体。
        if MediaRapidUploadQueryService.has_active_media(media.id):
            raise ApiError(
                409,
                "media_rapid_upload_in_progress",
                "媒体正在执行秒传，暂不能删除",
                {"media_id": media.id},
            )

        thumbnails = list(
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media == media)
        )
        thumbnail_image_ids = [thumbnail.image_id for thumbnail in thumbnails]

        # 删除语义对齐本地：删 Media = 文件也没了。cloud115 走 SDK 删（进回收站），本地 unlink。
        if cls.is_cloud115_media(media):
            cls._delete_cloud115_media_file(media)
        elif media.path:
            cls._delete_local_media_file(media)

        with get_database().atomic():
            # 依赖 DB 外键 CASCADE 自动清 MediaProgress / MediaPoint / MediaThumbnail。
            Media.delete().where(Media.id == media.id).execute()

            obsolete_image_paths: set[str] = set()
            for image_id in thumbnail_image_ids:
                image = Image.get_or_none(Image.id == image_id)
                obsolete_image_paths |= ImageCleanupService.delete_image_record_if_unused(image)

            ResourceTaskState.delete().where(
                ResourceTaskState.resource_type == "media",
                ResourceTaskState.resource_id == media.id,
            ).execute()

        ImageCleanupService.delete_obsolete_image_files(obsolete_image_paths)

        # 仅 JAV 媒体缩略图会进向量库；非 JAV 缩略图落 SKIPPED 从不入库，跳过空删省一次远端往返。
        if media.movie_number:
            try:
                get_qdrant_thumbnail_store().delete_by_media_id(media.id)
            except Exception as exc:
                logger.warning("Delete media vectors failed media_id={} detail={}", media.id, exc)

    @classmethod
    def list_thumbnails(cls, media_id: int) -> list[MediaThumbnailResource]:
        cls._require_media(media_id)
        return MediaThumbnailService.list_media_thumbnails(media_id)

    @classmethod
    def check_media_validity(cls, media_id: int) -> MediaValidityCheckResponse:
        cls._require_media(media_id)
        result = MediaFileScanService().check_media_file(media_id)
        return MediaValidityCheckResponse(
            id=result.id,
            path=result.path,
            file_exists=result.file_exists,
            valid_before=result.valid_before,
            valid_after=result.valid_after,
            updated=result.updated,
            invalidated=result.invalidated,
            revived=result.revived,
            checked_at=result.checked_at,
        )

    @staticmethod
    def _to_invalid_media_resource(media: Media) -> InvalidMediaResource:
        # 按归属拆分：JAV 媒体展示番号与影片封面，非 JAV 媒体回退到 VideoItem 标题。
        if media.movie_number:
            movie = media.movie
            return InvalidMediaResource(
                id=media.id,
                movie_number=movie.movie_number,
                movie_title=movie.title,
                cover_image=ImageResource.from_attributes_model(movie.cover_image)
                if movie.cover_image_id is not None
                else None,
                thin_cover_image=ImageResource.from_attributes_model(movie.thin_cover_image)
                if movie.thin_cover_image_id is not None
                else None,
                path=media.display_path,
                library_id=media.library_id,
                library_name=media.library.name if media.library_id is not None else None,
                file_size_bytes=media.file_size_bytes,
                updated_at=media.updated_at,
            )
        video_item = media.video_item if media.video_item_id else None
        return InvalidMediaResource(
            id=media.id,
            video_item_id=media.video_item_id,
            movie_title=video_item.title if video_item is not None else None,
            cover_image=ImageResource.from_attributes_model(video_item.cover_image)
            if video_item is not None and video_item.cover_image_id is not None
            else None,
            path=media.display_path,
            library_id=media.library_id,
            library_name=media.library.name if media.library_id is not None else None,
            file_size_bytes=media.file_size_bytes,
            updated_at=media.updated_at,
        )

    @classmethod
    def list_invalid_media(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> PageResponse[InvalidMediaResource]:
        validate_page(page, page_size, error_code="invalid_media_filter")
        # 非 JAV 媒体没有 movie，Movie 改为 LEFT OUTER，并补 VideoItem 兜底标题。
        base_query = Media.select(Media, Movie, VideoItem).join(
            Movie,
            peewee.JOIN.LEFT_OUTER,
            on=(Media.movie == Movie.movie_number),
        )
        # 失效媒体卡片需要展示影片横版与竖版封面，沿用影片卡片的关联加载逻辑。
        base_query, _thin_cover_alias = with_movie_card_relations(base_query)
        # 非 JAV 失效媒体封面来自 VideoItem.cover_image，用别名再 LEFT JOIN 一张 Image 预加载，
        # 避免 _to_invalid_media_resource 逐行懒加载 video_item.cover_image（N+1）。
        video_cover_alias = Image.alias()
        base_query = (
            base_query.select_extend(MediaLibrary, video_cover_alias)
            .switch(Media)
            .join(VideoItem, peewee.JOIN.LEFT_OUTER)
            .join(
                video_cover_alias,
                peewee.JOIN.LEFT_OUTER,
                on=(VideoItem.cover_image == video_cover_alias.id),
                attr="cover_image",
            )
            .switch(Media)
            .join(MediaLibrary, peewee.JOIN.LEFT_OUTER)
            .where(Media.valid == False)
        )
        normalized = (search or "").strip()
        if normalized:
            base_query = base_query.where(
                (Movie.movie_number.contains(normalized))
                | (Movie.title.contains(normalized))
                | (VideoItem.title.contains(normalized))
                | (Media.path.contains(normalized))
            )
        return paginate(
            base_query.order_by(Media.updated_at.desc(), Media.id.desc()),
            page,
            page_size,
            error_code="invalid_media_filter",
            item_mapper=cls._to_invalid_media_resource,
            response_model=PageResponse[InvalidMediaResource],
        )
