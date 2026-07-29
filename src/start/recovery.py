from __future__ import annotations

from typing import Callable

from loguru import logger

from src.service.catalog import (
    MovieDescTranslationService,
    MovieDescSyncService,
    MovieInteractionSyncService,
    MovieTitleTranslationService,
)
from src.service.playback import MediaThumbnailService
from src.service.system import ActivityService
from src.service.system.resource_task_runner import ResourceTaskLedger
from src.service.transfers import (
    Cloud115OfflineSyncService,
    DownloadSyncService,
    MediaImportJobService,
    MediaRapidUploadService,
)
from src.service.videos import VideoImportJobService

# 注册表: task_key -> 业务层回收 callable。
# 启动恢复在任务层 (BackgroundTaskRun) 回收之后，按 task_key 查表联动清理业务状态。
def _recover_media_directory_imports() -> dict:
    """回收普通目录导入，并让 Cloud115 下载导入回到可重试状态。"""
    result = MediaImportJobService.recover_orphaned_jobs()
    result["cloud115_download_import_recovered_count"] = (
        Cloud115OfflineSyncService.recover_interrupted_imports()
    )
    return result


BUSINESS_RECOVERY_HANDLERS: dict[str, Callable[[], object]] = {
    "movie_interaction_sync": lambda: MovieInteractionSyncService.recover_interrupted_running_movies(
        error_message=MovieInteractionSyncService.INTERRUPTED_SYNC_ERROR_MESSAGE,
    ),
    "movie_desc_sync": lambda: MovieDescSyncService.recover_interrupted_running_movies(
        error_message=MovieDescSyncService.INTERRUPTED_FETCH_ERROR_MESSAGE,
    ),
    "movie_desc_translation": lambda: MovieDescTranslationService.recover_interrupted_running_movies(
        error_message=MovieDescTranslationService.INTERRUPTED_TRANSLATION_ERROR_MESSAGE,
    ),
    "movie_title_translation": lambda: MovieTitleTranslationService.recover_interrupted_running_movies(
        error_message=MovieTitleTranslationService.INTERRUPTED_TRANSLATION_ERROR_MESSAGE,
    ),
    "media_thumbnail_generation": lambda: MediaThumbnailService.recover_interrupted_running_media(
        error_message=MediaThumbnailService.INTERRUPTED_GENERATION_ERROR_MESSAGE,
    ),
    # Wave 2：task_key 与资源状态 key 已合并，崩溃回收走 kernel 的 running 复位。
    "subscribed_movie_auto_download": lambda: ResourceTaskLedger.recover_running(
        "subscribed_movie_auto_download",
        error_message="订阅影片资源查询任务中断，等待重试",
    ),
    "download_task_import": lambda: DownloadSyncService().recover_orphaned_imports_only(),
    "media_directory_import": _recover_media_directory_imports,
    "video_directory_import": lambda: VideoImportJobService.recover_orphaned_jobs(),
    "media_rapid_upload": lambda: MediaRapidUploadService.recover_interrupted_batches(),
}

# 秒传批次需要在业务恢复完成、统计已收敛后才能发送一条汇总通知。
BUSINESS_MANAGED_NOTIFICATION_TASK_KEYS = {"media_rapid_upload"}


def recover_interrupted_tasks(
    *,
    trigger_types: tuple[str, ...],
    error_message: str,
) -> set[str]:
    """启动时回收中断的任务并联动清理业务状态。

    Phase 1: 按 trigger_type 逐一扫描 pending/running 的 BackgroundTaskRun，标记为 failed。
    Phase 2: 对回收到的 task_key，查注册表调用对应的业务层回收逻辑。
    """
    recovered_task_keys: set[str] = set()
    for trigger_type in trigger_types:
        for task_run in ActivityService.recover_interrupted_task_runs(
            trigger_type=trigger_type,
            error_message=error_message,
            allow_null_owner=True,
            force=True,
            suppress_notification_task_keys=BUSINESS_MANAGED_NOTIFICATION_TASK_KEYS,
        ):
            recovered_task_keys.add(task_run.task_key)

    # 按注册表的插入顺序遍历，保证回收顺序确定性。
    for task_key, handler in BUSINESS_RECOVERY_HANDLERS.items():
        if task_key in recovered_task_keys:
            logger.info("Recovering business state for task_key={}", task_key)
            handler()

    return recovered_task_keys
