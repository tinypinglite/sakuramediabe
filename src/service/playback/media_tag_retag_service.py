from __future__ import annotations

from typing import Any

from loguru import logger

from src.i18n import translate
from src.model import Media, MediaMediaTag, get_database
from src.service.playback.media_tag_service import MediaTagService


class MediaTagRetagService:
    TASK_KEY = "media_tag_retag"

    @staticmethod
    def _jav_media_ids() -> list[int]:
        return [
            int(media_id)
            for (media_id,) in (
                Media.select(Media.id)
                .where(Media.movie.is_null(False))
                .order_by(Media.id)
                .tuples()
            )
        ]

    @classmethod
    def retag_all_jav_media(cls, *, reporter) -> dict[str, Any]:
        media_ids = cls._jav_media_ids()
        rules = MediaTagService.load_active_rules()
        stats: dict[str, Any] = {
            "total_media": len(media_ids),
            "tagged_media": 0,
            "updated_media": 0,
            "cleared_media": 0,
            "failed_media": 0,
        }
        logger.info(
            "Media tag retag started total_media={} active_rules={}",
            len(media_ids),
            len(rules),
        )
        step = max(len(media_ids) // 20, 1)

        def emit_progress(completed: int) -> None:
            reporter.emit(
                current=completed,
                total=len(media_ids),
                text=translate(
                    "tasks.media_tag_retag.progress.completed",
                    current=completed,
                    total=len(media_ids),
                    tagged=stats["tagged_media"],
                    updated=stats["updated_media"],
                    cleared=stats["cleared_media"],
                    failed=stats["failed_media"],
                ),
                summary_patch=stats,
            )

        if not media_ids:
            emit_progress(0)
            return stats

        for completed, media_id in enumerate(media_ids, start=1):
            try:
                with get_database().atomic():
                    media = Media.get_by_id(media_id)
                    old_tag_ids = {
                        int(tag_id)
                        for (tag_id,) in (
                            MediaMediaTag.select(MediaMediaTag.tag)
                            .where(MediaMediaTag.media == media_id)
                            .tuples()
                        )
                    }
                    new_tag_ids = MediaTagService.replace_media_tags(
                        media,
                        rules,
                        release_title=media.release_title,
                        release_description=media.release_description,
                    )
                    if new_tag_ids:
                        stats["tagged_media"] += 1
                    if old_tag_ids != new_tag_ids:
                        stats["updated_media"] += 1
                    if old_tag_ids and not new_tag_ids:
                        stats["cleared_media"] += 1
            except Exception as exc:
                stats["failed_media"] += 1
                logger.warning(
                    "Media tag retag failed media_id={} detail={}",
                    media_id,
                    exc,
                )
            emit_progress(completed)
            if completed == 1 or completed % step == 0:
                logger.info(
                    "Media tag retag progress completed={}/{} tagged={} updated={} cleared={} failed={}",
                    completed,
                    len(media_ids),
                    stats["tagged_media"],
                    stats["updated_media"],
                    stats["cleared_media"],
                    stats["failed_media"],
                )
        return stats
