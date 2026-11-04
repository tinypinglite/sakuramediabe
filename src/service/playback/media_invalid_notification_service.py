from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from src.i18n import translate
from src.schema.system.activity import NotificationResource
from src.service.system.activity import NotificationDraft, NotificationService

REASON_CATALOG_KEYS = {
    "file_missing": "media.invalid.reason.file_missing",
    "container_invalid": "media.invalid.reason.container_invalid",
    "moov_missing": "media.invalid.reason.moov_missing",
    "video_stream_missing": "media.invalid.reason.video_stream_missing",
    "interleave_bad": "media.invalid.reason.interleave_bad",
}


class MediaInvalidNotificationService:
    MAX_LISTED_ITEMS = 5

    @classmethod
    def _reason_label(cls, reason: Any) -> str:
        catalog_key = (
            REASON_CATALOG_KEYS.get(reason)
            if isinstance(reason, str)
            else None
        )
        return translate(catalog_key or "media.invalid.reason.unknown")

    @classmethod
    def _items_text(cls, items: list[dict[str, Any]]) -> str:
        rendered = [
            translate(
                "notifications.media_invalid.item",
                name=str(item.get("display_name") or ""),
                reason=cls._reason_label(item.get("reason")),
            )
            for item in items[: cls.MAX_LISTED_ITEMS]
        ]
        text = ", ".join(rendered)
        remaining = len(items) - len(rendered)
        if remaining > 0:
            text = (
                f"{text} "
                f"{translate('notifications.media_invalid.more', count=remaining)}"
            )
        return text

    @classmethod
    def notify_new_invalid(
        cls,
        *,
        items: Iterable[dict[str, Any]],
        task_run_id: int | None = None,
    ) -> NotificationResource | None:
        unique_items: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for item in items:
            media_id = item.get("media_id")
            if not isinstance(media_id, int) or isinstance(media_id, bool):
                continue
            if media_id in seen_ids:
                continue
            seen_ids.add(media_id)
            unique_items.append(item)
        if not unique_items:
            return None

        draft = NotificationDraft(
            category="warning",
            title=translate("notifications.media_invalid.title"),
            content=translate(
                "notifications.media_invalid.content",
                count=len(unique_items),
                items=cls._items_text(unique_items),
            ),
            event_type="media_invalid_detected",
            resource_type="media_management",
            related_task_run_id=task_run_id,
        )
        if task_run_id is None:
            return NotificationService.create(draft)
        return NotificationService.create_once(
            replace(
                draft,
                dedupe_key=f"media_invalid_detected:task_run:{task_run_id}",
            )
        )
