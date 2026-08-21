"""Media 缩略图任务状态的生命周期操作。"""

from __future__ import annotations

from src.model import Media, MediaThumbnail


def thumbnail_state_reset_values(media: Media) -> dict:
    """返回媒体重新可用时应写入的缩略图状态。

    只有没有成功产物的媒体才回到 pending；已有缩略图的记录保持 succeeded，避免巡检
    复活一个旧文件后在列表上显示“待生成”却永远不会进入生成候选集。
    """
    has_thumbnail = MediaThumbnail.select(MediaThumbnail.id).where(
        MediaThumbnail.media == media.id
    ).exists()
    return {
        Media.thumbnail_generation_state: (
            Media.THUMBNAIL_STATE_SUCCEEDED
            if has_thumbnail
            else Media.THUMBNAIL_STATE_PENDING
        ),
        Media.thumbnail_attempt_count: 0,
        Media.thumbnail_deferred_count: 0,
        Media.thumbnail_next_retry_at: None,
        Media.thumbnail_last_error_code: None,
        Media.thumbnail_last_error: None,
        Media.thumbnail_terminal_at: None,
        Media.thumbnail_source_fingerprint: media.content_fingerprint,
    }


def reset_thumbnail_state_for_revival(media: Media) -> None:
    """原地重置已存在 Media 的缩略图状态，调用方负责保存。"""
    for field, value in thumbnail_state_reset_values(media).items():
        setattr(media, field.name, value)
