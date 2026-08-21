from dataclasses import replace
from typing import Any

from src.schema.system.activity import NotificationResource
from src.service.system.activity import NotificationDraft, NotificationService


def create_new_media_reminder(
    *,
    movie_items: list[dict[str, Any]],
    related_task_run_id: int | None = None,
) -> NotificationResource | None:
    """汇总本次导入新增影片，避免按影片逐条刷通知。"""
    unique_items: list[dict[str, Any]] = []
    seen_movie_numbers: set[str] = set()
    for item in movie_items:
        movie_number = str(item.get("movie_number") or "").strip()
        if not movie_number or movie_number in seen_movie_numbers:
            continue
        seen_movie_numbers.add(movie_number)
        unique_items.append(item)
    if not unique_items:
        return None

    sample_text = "、".join(
        item.get("movie_number") or "" for item in unique_items[:3]
    )
    if len(unique_items) > 3:
        sample_text = f"{sample_text} 等 {len(unique_items)} 部影片"
    related_resource_id = unique_items[0].get("movie_id")
    draft = NotificationDraft(
        category="reminder",
        title="有新的影片可以播放了",
        content=f"本次后台处理新增可播放影片 {len(unique_items)} 部：{sample_text}",
        related_task_run_id=related_task_run_id,
        related_resource_type="movie",
        related_resource_id=(
            related_resource_id if isinstance(related_resource_id, int) else None
        ),
    )
    if related_task_run_id is None:
        # 保留通用入口的旧行为；下载导入链路始终会提供 task run。
        return NotificationService.create(draft)
    return NotificationService.create_once(
        replace(
            draft,
            event_type="download_import_new_media",
            dedupe_key=f"download_import_new_media:task_run:{related_task_run_id}",
            resource_type="background_task_run",
            resource_id=related_task_run_id,
        )
    )
