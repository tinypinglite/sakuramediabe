from src.schema.system.activity import NotificationResource
from src.service.system.activity import NotificationDraft, NotificationService


def create_media_rapid_upload_notification(
    *,
    batch_id: int,
    task_run_id: int,
    total_count: int,
    succeeded_count: int,
    failed_count: int,
    cleanup_failed_count: int,
    batch_failed: bool = False,
) -> NotificationResource:
    """为一个批量秒传批次发送唯一的终态汇总通知。"""
    has_failures = failed_count > 0 or cleanup_failed_count > 0
    if batch_failed:
        category = "error"
        title = "批量秒传任务失败"
    else:
        category = "warning" if has_failures else "info"
        title = "批量秒传完成（部分失败）" if has_failures else "批量秒传完成"
    content = (
        f"本批次共 {total_count} 个媒体：成功 {succeeded_count} 个，"
        f"失败 {failed_count} 个，本地文件清理失败 {cleanup_failed_count} 个。"
    )
    return NotificationService.create(
        NotificationDraft(
            category=category,
            title=title,
            content=content,
            related_task_run_id=task_run_id,
            related_resource_type="media_rapid_upload_batch",
            related_resource_id=batch_id,
        )
    )
