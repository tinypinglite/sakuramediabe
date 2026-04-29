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
from src.service.transfers import DownloadSyncService

# 注册表: task_key -> 业务层回收 callable。
# 启动恢复在任务层 (BackgroundTaskRun) 回收之后，按 task_key 查表联动清理业务状态。
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
    "download_task_import": lambda: DownloadSyncService().recover_orphaned_imports_only(),
}


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
        ):
            recovered_task_keys.add(task_run.task_key)

    # 按注册表的插入顺序遍历，保证回收顺序确定性。
    for task_key, handler in BUSINESS_RECOVERY_HANDLERS.items():
        if task_key in recovered_task_keys:
            logger.info("Recovering business state for task_key={}", task_key)
            handler()

    return recovered_task_keys
