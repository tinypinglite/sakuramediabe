"""视频条目（VideoItem）service：非 JAV 视频的条目增删改查与详情组装。"""

from datetime import datetime

from peewee import JOIN, Case, fn

from src.api.exception.errors import ApiError
from src.common import build_signed_media_url
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_by_id, validate_page
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
    get_database,
)
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import (
    MovieMediaPointResource,
    MovieMediaProgressResource,
    MovieMediaResource,
)
from src.schema.common.pagination import PageResponse
from src.schema.videos.items import (
    VideoCollectionRef,
    VideoItemCreateRequest,
    VideoItemDetailResource,
    VideoItemListItemResource,
    VideoItemUpdateRequest,
)


class VideoItemService:
    @staticmethod
    def _current_time() -> datetime:
        return utc_now_for_db()

    @staticmethod
    def _require_video(video_id: int) -> VideoItem:
        return require_by_id(VideoItem, video_id, "video_item", error_message="Video item not found")

    @staticmethod
    def _parse_resolution(value: str | None) -> tuple[int | None, int | None]:
        """拆 Media.resolution 字符串（形如 "1920x1080"）为 (width, height)。

        空 / 缺 'x' / 非整数 / 非正值 一律返 (None, None)，由调用方决定回退策略（前端
        瀑布流回退 16:9）。MediaMetadataProbeService 探测失败时本就不写该字段。
        """
        if not value:
            return (None, None)
        parts = value.split("x", 1)
        if len(parts) != 2:
            return (None, None)
        try:
            width = int(parts[0])
            height = int(parts[1])
        except ValueError:
            return (None, None)
        if width <= 0 or height <= 0:
            return (None, None)
        return (width, height)

    @staticmethod
    def _first_media_alias():
        """构造取「条目第一条媒体」的连接素材：每条目 MIN(Media.id) 分组子查询 + Media 别名。

        返回 (first_media 别名, first_media_id 子查询)，两者配合 LEFT JOIN 后，
        first_media 即每个条目按 Media.id 升序的第一条媒体，其时长/大小同时供排序与展示。
        """
        first_media_id = Media.select(
            Media.video_item.alias("owner_id"),
            fn.MIN(Media.id).alias("first_media_id"),
        ).group_by(Media.video_item)
        return Media.alias(), first_media_id

    @classmethod
    def _build_video_order(
        cls,
        sort: str | None,
        *,
        first_media,
        default_sort: str,
        extra_columns: dict | None = None,
        tie_breaker=None,
    ):
        """解析 `field:direction` 排序，返回 [主排序, 次序稳定项]。

        - created_at / title 取 VideoItem 自身列；
        - duration / file_size 取第一条媒体的时长 / 文件大小，无媒体时按 0 参与排序
          （COALESCE 兜底，保证空媒体排序稳定）；
        - extra_columns 注入域特有字段（合集的 position）。
        """
        columns = {
            "created_at": VideoItem.created_at,
            "title": VideoItem.title,
            "duration": fn.COALESCE(first_media.duration_seconds, 0),
            "file_size": fn.COALESCE(first_media.file_size_bytes, 0),
        }
        if extra_columns:
            columns.update(extra_columns)
        normalized = (sort or default_sort).strip().lower()
        try:
            field_name, direction = normalized.split(":", 1)
        except ValueError as exc:
            raise ApiError(422, "invalid_video_filter", "Invalid video sort", {"sort": sort}) from exc
        if field_name not in columns or direction not in ("asc", "desc"):
            raise ApiError(422, "invalid_video_filter", "Invalid video sort", {"sort": sort})
        is_asc = direction == "asc"
        ordered = columns[field_name].asc() if is_asc else columns[field_name].desc()
        breaker = VideoItem.id if tie_breaker is None else tie_breaker
        tie = breaker.asc() if is_asc else breaker.desc()
        return [ordered, tie]

    @classmethod
    def _filtered_query(
        cls,
        *,
        query: str | None = None,
    ):
        video_query = VideoItem.select(VideoItem)
        if query is not None:
            normalized = query.strip()
            if not normalized:
                raise ApiError(422, "invalid_video_filter", "Invalid video filter", {"query": query})
            video_query = video_query.where(VideoItem.title.contains(normalized))
        return video_query

    @staticmethod
    def _media_stats(video_ids: list[int]) -> dict[int, tuple[int, bool]]:
        """批量统计每个视频的媒体数量与是否存在可播放媒体。"""
        if not video_ids:
            return {}
        # 用 CASE 折算 valid 计数，避免 PostgreSQL 不支持 SUM(boolean)。
        valid_count = fn.SUM(Case(None, [(Media.valid == True, 1)], 0))
        rows = (
            Media.select(
                Media.video_item,
                fn.COUNT(Media.id).alias("media_count"),
                valid_count.alias("valid_count"),
            )
            .where(Media.video_item.in_(video_ids))
            .group_by(Media.video_item)
        )
        stats: dict[int, tuple[int, bool]] = {}
        for row in rows:
            stats[row.video_item_id] = (row.media_count, bool(row.valid_count))
        return stats

    @staticmethod
    def _collections_map(video_ids: list[int]) -> dict[int, list[VideoCollectionRef]]:
        """批量拉每个视频归属的合集列表，按合集名称升序返回 {video_item_id: [Ref, ...]}。

        用于列表/详情共用的合集引用填充，避免逐条懒加载导致 N+1。
        """
        if not video_ids:
            return {}
        rows = (
            VideoCollectionItem.select(
                VideoCollectionItem.video_item,
                VideoCollection.id,
                VideoCollection.name,
            )
            .join(VideoCollection, on=(VideoCollectionItem.collection == VideoCollection.id))
            .where(VideoCollectionItem.video_item.in_(video_ids))
            .order_by(VideoCollection.name.asc(), VideoCollection.id.asc())
        )
        result: dict[int, list[VideoCollectionRef]] = {}
        for row in rows:
            result.setdefault(row.video_item_id, []).append(
                VideoCollectionRef(id=row.collection.id, name=row.collection.name)
            )
        return result

    @classmethod
    def _to_list_item(
        cls,
        video: VideoItem,
        media_count: int,
        can_play: bool,
        *,
        duration_seconds: int = 0,
        file_size_bytes: int = 0,
        cover_width: int | None = None,
        cover_height: int | None = None,
        collections: list[VideoCollectionRef] | None = None,
    ) -> VideoItemListItemResource:
        return VideoItemListItemResource(
            id=video.id,
            title=video.title,
            summary=video.summary,
            cover_image=ImageResource.from_attributes_model(video.cover_image)
            if video.cover_image_id is not None
            else None,
            release_date=video.release_date,
            duration_seconds=duration_seconds,
            file_size_bytes=file_size_bytes,
            cover_width=cover_width,
            cover_height=cover_height,
            media_count=media_count,
            can_play=can_play,
            collections=collections or [],
            created_at=video.created_at,
            updated_at=video.updated_at,
        )

    @classmethod
    def list_videos(
        cls,
        *,
        query: str | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[VideoItemListItemResource]:
        validate_page(page, page_size, error_code="invalid_video_filter")
        base_query = cls._filtered_query(query=query)
        total = base_query.count()
        first_media, first_media_id = cls._first_media_alias()
        order_by = cls._build_video_order(
            sort, first_media=first_media, default_sort="created_at:desc"
        )
        # 一次 LEFT JOIN 预加载封面与「第一条媒体」的时长/大小：既供排序也供展示，
        # 避免 _to_list_item 逐行懒加载 cover_image（N+1）与额外的聚合查询。
        videos = list(
            base_query.select_extend(
                Image,
                fn.COALESCE(first_media.duration_seconds, 0).alias("first_duration_seconds"),
                fn.COALESCE(first_media.file_size_bytes, 0).alias("first_file_size_bytes"),
                fn.COALESCE(first_media.resolution, "").alias("first_resolution"),
            )
            .join(Image, JOIN.LEFT_OUTER, on=(VideoItem.cover_image == Image.id))
            .switch(VideoItem)
            .join(first_media_id, JOIN.LEFT_OUTER, on=(first_media_id.c.owner_id == VideoItem.id))
            .join(first_media, JOIN.LEFT_OUTER, on=(first_media.id == first_media_id.c.first_media_id))
            .order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        video_ids = [video.id for video in videos]
        stats = cls._media_stats(video_ids)
        collections_map = cls._collections_map(video_ids)
        items = []
        for video in videos:
            cover_width, cover_height = cls._parse_resolution(video.first_resolution)
            items.append(
                cls._to_list_item(
                    video,
                    *stats.get(video.id, (0, False)),
                    duration_seconds=video.first_duration_seconds,
                    file_size_bytes=video.first_file_size_bytes,
                    cover_width=cover_width,
                    cover_height=cover_height,
                    collections=collections_map.get(video.id, []),
                )
            )
        return PageResponse[VideoItemListItemResource](
            items=items,
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def _media_items(video: VideoItem) -> list[MovieMediaResource]:
        """组装视频详情页的媒体列表，复用影片媒体资源结构（进度 + 时刻）。"""
        from src.service.playback.media_service import MediaService

        media_items = list(
            Media.select(Media, MediaLibrary)
            .join(MediaLibrary, JOIN.LEFT_OUTER)
            .where(Media.video_item == video)
            .order_by(Media.id)
        )
        if not media_items:
            return []
        media_ids = [media.id for media in media_items]
        progress_items = {
            progress.media_id: progress
            for progress in MediaProgress.select(MediaProgress).where(MediaProgress.media.in_(media_ids))
        }
        points_by_media_id: dict[int, list[MovieMediaPointResource]] = {}
        point_query = (
            MediaPoint.select(MediaPoint, MediaThumbnail, Image)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
            .where(MediaPoint.media.in_(media_ids))
            .order_by(MediaPoint.media, MediaPoint.id)
        )
        for point in point_query:
            points_by_media_id.setdefault(point.media_id, []).append(
                MovieMediaPointResource(
                    point_id=point.id,
                    thumbnail_id=point.thumbnail_id,
                    offset_seconds=point.offset_seconds,
                    image=ImageResource.from_attributes_model(point.thumbnail.image),
                )
            )
        resources: list[MovieMediaResource] = []
        for media in media_items:
            progress = progress_items.get(media.id)
            media.progress = (
                None
                if progress is None
                else MovieMediaProgressResource(
                    last_position_seconds=progress.position_seconds,
                    last_watched_at=progress.last_watched_at,
                )
            )
            media.points = points_by_media_id.get(media.id, [])
            media.play_url = build_signed_media_url(media.id)
            media.library_backend = (
                "cloud115"
                if MediaService.is_cloud115_media(media)
                else ("local" if media.library_id is not None else None)
            )
            resources.append(MovieMediaResource.from_attributes_model(media))
        return resources

    @classmethod
    def get_video_detail(cls, video_id: int) -> VideoItemDetailResource:
        video = cls._require_video(video_id)
        media_items = cls._media_items(video)
        stats_media_count = len(media_items)
        can_play = any(media.valid for media in media_items)
        # 时长/大小取第一条媒体（media_items 已按 Media.id 升序），无媒体时为 0。
        first_media = media_items[0] if media_items else None
        cover_width, cover_height = cls._parse_resolution(
            first_media.resolution if first_media else None
        )
        collections = cls._collections_map([video.id]).get(video.id, [])
        return VideoItemDetailResource(
            id=video.id,
            title=video.title,
            summary=video.summary,
            cover_image=ImageResource.from_attributes_model(video.cover_image)
            if video.cover_image_id is not None
            else None,
            release_date=video.release_date,
            duration_seconds=first_media.duration_seconds if first_media else 0,
            file_size_bytes=first_media.file_size_bytes if first_media else 0,
            cover_width=cover_width,
            cover_height=cover_height,
            media_count=stats_media_count,
            can_play=can_play,
            collections=collections,
            created_at=video.created_at,
            updated_at=video.updated_at,
            media_items=media_items,
        )

    @classmethod
    def create_video(cls, payload: VideoItemCreateRequest) -> VideoItemDetailResource:
        video = VideoItem.create(
            title=payload.title,
            summary=payload.summary,
            release_date=payload.release_date,
        )
        return cls.get_video_detail(video.id)

    @classmethod
    def update_video(cls, video_id: int, payload: VideoItemUpdateRequest) -> VideoItemDetailResource:
        video = cls._require_video(video_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(422, "validation_error", "At least one field must be provided")
        if "title" in update_data and update_data["title"] is not None:
            video.title = update_data["title"]
        if "summary" in update_data and update_data["summary"] is not None:
            video.summary = update_data["summary"]
        if "release_date" in update_data:
            video.release_date = update_data["release_date"]
        video.updated_at = cls._current_time()
        video.save()
        return cls.get_video_detail(video.id)

    @classmethod
    def delete_video(cls, video_id: int) -> None:
        # 延迟导入避免与 media_service / image_cleanup_service 顶层依赖链形成循环。
        from src.service.catalog.image_cleanup_service import ImageCleanupService
        from src.service.playback.media_service import MediaService

        video = cls._require_video(video_id)
        # 复用媒体删除链路：清理文件、缩略图图片、向量与级联子表，而非简单置空可空外键。
        media_ids = [media.id for media in Media.select(Media.id).where(Media.video_item == video)]
        for media_id in media_ids:
            MediaService.delete_media(media_id)
        # 封面 Image 为该视频独有（generate_cover 新建），随视频一并清理图片行与磁盘文件，避免孤儿。
        cover_image = video.cover_image if video.cover_image_id is not None else None
        obsolete_image_paths: set[str] = set()
        with get_database().atomic():
            # 剩余的合集关联均为非空外键，recursive 删除即可清理。
            video.delete_instance(recursive=True)
            if cover_image is not None:
                obsolete_image_paths |= ImageCleanupService.delete_image_record_if_unused(cover_image)
        ImageCleanupService.delete_obsolete_image_files(obsolete_image_paths)
