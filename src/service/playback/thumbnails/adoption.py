"""缩略图归档与收养：删除 Media 时保留产物，同 hash 再次入库时零生成复用。"""

from __future__ import annotations

from loguru import logger

from src.common.image_store import read_image_bytes
from src.common.runtime_time import utc_now_for_db
from src.model import (
    Image,
    Media,
    MediaThumbnail,
    MediaThumbnailArchive,
    get_database,
)


class ThumbnailAdoptionService:
    """按文件指纹保存和复用缩略图，避免同内容文件反复远端抽帧。"""

    @staticmethod
    def _index_status_for(media: Media) -> int:
        # 与非 JAV 缩略图落 SKIPPED 的既有口径一致，避免向量索引长期滞留 PENDING。
        return (
            MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
            if media.movie_number
            else MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SKIPPED
        )

    @staticmethod
    def _image_readable(image_id: int) -> bool:
        image = Image.get_or_none(Image.id == image_id)
        if image is None or not image.origin:
            return False
        try:
            read_image_bytes(image.origin)
        except Exception as exc:
            logger.warning(
                "Adoptable thumbnail image unreadable image_id={} origin={} detail={}",
                image_id,
                image.origin,
                exc,
            )
            return False
        return True

    @classmethod
    def archive_media_thumbnails(cls, media: Media) -> int:
        """把待删 Media 的缩略图写进归档；返回新增归档条数。

        调用方在删除 Media 行之前执行：归档行持有 Image 引用，后续清理不会删除
        缩略图记录与物理文件，同 hash 文件再次入库即可收养这批产物。
        """
        if not media.file_hash:
            return 0
        entries = list(
            MediaThumbnail.select(MediaThumbnail.offset, MediaThumbnail.image_id)
            .where(MediaThumbnail.media == media.id)
            .tuples()
        )
        if not entries:
            return 0

        now = utc_now_for_db()
        rows = [
            {
                MediaThumbnailArchive.file_hash: media.file_hash,
                MediaThumbnailArchive.offset: int(offset),
                MediaThumbnailArchive.image: image_id,
                MediaThumbnailArchive.created_at: now,
                MediaThumbnailArchive.updated_at: now,
            }
            for offset, image_id in entries
        ]
        # 同 hash 多份拷贝反复删除时靠唯一约束幂等；RETURNING 只回新插入的条目。
        query = (
            MediaThumbnailArchive.insert_many(rows)
            .on_conflict_ignore()
            .returning(MediaThumbnailArchive.id)
        )
        return sum(1 for _ in query.execute())

    @classmethod
    def adopt_for_media(cls, media: Media) -> int:
        """为缺缩略图的 Media 复用同 hash 已有产物；成功返回收养条数。"""
        if not media.file_hash or media.thumbnail_adoption_blocked:
            return 0
        exists = (
            MediaThumbnail.select(MediaThumbnail.id)
            .where(MediaThumbnail.media == media.id)
            .exists()
        )
        if exists:
            return 0
        entries = cls._live_sibling_entries(media) or cls._archived_entries(media)
        if not entries:
            return 0

        index_status = cls._index_status_for(media)
        with get_database().atomic():
            for offset, image_id in entries:
                MediaThumbnail.create(
                    media=media,
                    image=image_id,
                    offset=offset,
                    image_search_index_status=index_status,
                )
            Media.update(
                thumbnail_generation_state=Media.THUMBNAIL_STATE_SUCCEEDED,
                thumbnail_attempt_count=0,
                thumbnail_deferred_count=0,
                thumbnail_next_retry_at=None,
                thumbnail_last_error_code=None,
                thumbnail_last_error=None,
                thumbnail_terminal_at=None,
                updated_at=utc_now_for_db(),
            ).where(Media.id == media.id).execute()
        media.thumbnail_generation_state = Media.THUMBNAIL_STATE_SUCCEEDED
        logger.info(
            "Media thumbnails adopted media_id={} file_hash={} count={}",
            media.id,
            media.file_hash,
            len(entries),
        )
        return len(entries)

    @classmethod
    def _live_sibling_entries(cls, media: Media) -> list[tuple[int, int]]:
        """优先复用存活副本的缩略图；同归属的兄弟排在前面。"""
        sibling_rows = list(
            Media.select(Media.id, Media.movie_number, Media.video_item_id)
            .where(
                Media.id != media.id,
                Media.file_hash == media.file_hash,
                Media.id.in_(MediaThumbnail.select(MediaThumbnail.media)),
            )
            .order_by(Media.valid.desc(), Media.updated_at.desc(), Media.id.desc())
            .tuples()
        )
        same_owner: list[int] = []
        others: list[int] = []
        for sibling_id, movie_number, video_item_id in sibling_rows:
            if (
                movie_number == media.movie_number
                and video_item_id == media.video_item_id
            ):
                same_owner.append(int(sibling_id))
            else:
                others.append(int(sibling_id))
        for sibling_id in (*same_owner, *others):
            entries = cls._thumbnail_entries(sibling_id)
            if entries and cls._image_readable(entries[0][1]):
                return entries
        return []

    @classmethod
    def _archived_entries(cls, media: Media) -> list[tuple[int, int]]:
        entries = [
            (int(offset), int(image_id))
            for offset, image_id in (
                MediaThumbnailArchive.select(
                    MediaThumbnailArchive.offset,
                    MediaThumbnailArchive.image_id,
                )
                .where(MediaThumbnailArchive.file_hash == media.file_hash)
                .order_by(MediaThumbnailArchive.offset.asc())
                .tuples()
            )
        ]
        if not entries or not cls._image_readable(entries[0][1]):
            return []
        return entries

    @staticmethod
    def _thumbnail_entries(media_id: int) -> list[tuple[int, int]]:
        return [
            (int(offset), int(image_id))
            for offset, image_id in (
                MediaThumbnail.select(MediaThumbnail.offset, MediaThumbnail.image_id)
                .where(MediaThumbnail.media == media_id)
                .order_by(MediaThumbnail.offset.asc())
                .tuples()
            )
        ]


__all__ = ["ThumbnailAdoptionService"]
