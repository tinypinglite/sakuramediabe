"""队列专属任务注册表（任务架构 Wave 3，见 docs/development/task-architecture.md）。

不走 cron 的执行链路（导入族 / 秒传 / 单资源手动任务）由 API 入队、worker 按本表
解析执行。handler 签名 ``(reporter, params) -> dict``，运行在 worker 的 run_task 内，
抛异常即任务失败（领域侧终态由 handler 自己收口后 re-raise）。

lane 划分并发道：``import`` 道复刻旧 ``DownloadImportRunner``（2 并发），
``rapid_upload`` 道复刻旧 ``MediaRapidUploadRunner``（2 并发），其余 default（4 并发）。
与 JOB_REGISTRY 同 key 的条目（单资源手动任务）只在运行带 params 时生效，
否则按 cron 批任务解析。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

LANE_DEFAULT = "default"
LANE_IMPORT = "import"
LANE_RAPID_UPLOAD = "rapid_upload"

# 并发道容量：default 复刻 APS ThreadPoolExecutor(4)，两条专属道复刻旧线程池的 2 并发。
LANE_CONCURRENCY: dict[str, int] = {
    LANE_DEFAULT: 4,
    LANE_IMPORT: 2,
    LANE_RAPID_UPLOAD: 2,
}


@dataclass(frozen=True)
class QueueTaskDefinition:
    task_key: str
    log_name: str
    handler: Callable[[Any, dict], Any]
    lane: str = LANE_DEFAULT
    notify_result: bool = True


def _run_media_directory_import(reporter, params: dict) -> dict:
    from src.service.transfers.media_import_job_service import import_job_service_for

    job_id = int(params["import_job_id"])
    return import_job_service_for(job_id).execute_from_queue(reporter, params)


def _run_video_directory_import(reporter, params: dict) -> dict:
    from src.service.videos import video_import_job_service_for

    job_id = int(params["video_import_job_id"])
    return video_import_job_service_for(job_id).execute_from_queue(reporter, params)


def _run_download_task_import(reporter, params: dict) -> dict:
    from src.service.transfers.download_task_service import DownloadTaskService

    return DownloadTaskService.execute_import_from_queue(reporter, params)


def _run_media_rapid_upload(reporter, params: dict) -> dict:
    # 必须走 facade：executor 内部依赖 state_machine / item_executor 的方法，
    # 只有多继承组合后的 MediaRapidUploadService 才解析得到。
    from src.service.transfers.media_rapid_upload.facade import MediaRapidUploadService

    return MediaRapidUploadService.execute_batch_from_queue(reporter, params)


def _run_movie_desc_translation_subset(reporter, params: dict) -> dict:
    from src.service.catalog.movie_desc_translation_service import MovieDescTranslationService

    return MovieDescTranslationService().run(reporter=reporter, only_ids=params.get("only_ids"))


def _run_movie_interaction_sync_subset(reporter, params: dict) -> dict:
    from src.service.catalog.movie_interaction_sync_service import MovieInteractionSyncService

    return MovieInteractionSyncService().run(reporter=reporter, only_ids=params.get("only_ids"))


def _run_movie_title_translation_subset(reporter, params: dict) -> dict:
    from src.service.catalog.movie_title_translation_service import MovieTitleTranslationService

    return MovieTitleTranslationService().run(reporter=reporter, only_ids=params.get("only_ids"))


def _run_movie_desc_sync_subset(reporter, params: dict) -> dict:
    from src.service.catalog.movie_desc_sync_service import MovieDescSyncService

    return MovieDescSyncService().run(reporter=reporter, only_ids=params.get("only_ids"))


def _run_media_thumbnail_generation_subset(reporter, params: dict) -> dict:
    from src.service.playback import MediaThumbnailService

    return MediaThumbnailService.generate_pending_thumbnails(
        reporter=reporter, only_ids=params.get("only_ids")
    )


def _run_subscribed_movie_auto_download_subset(reporter, params: dict) -> dict:
    from src.service.transfers.subscribed_movie_auto_download_service import (
        SubscribedMovieAutoDownloadService,
    )

    return SubscribedMovieAutoDownloadService().run(
        reporter=reporter, only_ids=params.get("only_ids")
    )


QUEUE_TASK_REGISTRY: dict[str, QueueTaskDefinition] = {
    definition.task_key: definition
    for definition in (
        QueueTaskDefinition(
            task_key="media_directory_import",
            log_name="media-directory-import",
            handler=_run_media_directory_import,
            lane=LANE_IMPORT,
        ),
        QueueTaskDefinition(
            task_key="video_directory_import",
            log_name="video-directory-import",
            handler=_run_video_directory_import,
            lane=LANE_IMPORT,
        ),
        QueueTaskDefinition(
            task_key="download_task_import",
            log_name="download-task-import",
            handler=_run_download_task_import,
            lane=LANE_IMPORT,
        ),
        QueueTaskDefinition(
            task_key="media_rapid_upload",
            log_name="media-rapid-upload",
            handler=_run_media_rapid_upload,
            lane=LANE_RAPID_UPLOAD,
            # 批次完成通知由业务侧幂等发送（含崩溃恢复补发），任务级通知关闭。
            notify_result=False,
        ),
        # 子集手动任务：与 cron 批任务同 key，仅当运行带 params 时命中
        # （统一 action 端点的 retry_now/rerun 走这里；only_ids 即强制语义）。
        QueueTaskDefinition(
            task_key="movie_desc_translation",
            log_name="movie-desc-translation",
            handler=_run_movie_desc_translation_subset,
        ),
        QueueTaskDefinition(
            task_key="movie_interaction_sync",
            log_name="movie-interaction-sync",
            handler=_run_movie_interaction_sync_subset,
        ),
        QueueTaskDefinition(
            task_key="movie_title_translation",
            log_name="movie-title-translation",
            handler=_run_movie_title_translation_subset,
        ),
        QueueTaskDefinition(
            task_key="movie_desc_sync",
            log_name="movie-desc-sync",
            handler=_run_movie_desc_sync_subset,
        ),
        QueueTaskDefinition(
            task_key="media_thumbnail_generation",
            log_name="media-thumbnail-generation",
            handler=_run_media_thumbnail_generation_subset,
        ),
        QueueTaskDefinition(
            task_key="subscribed_movie_auto_download",
            log_name="subscribed-movie-auto-download",
            handler=_run_subscribed_movie_auto_download_subset,
        ),
    )
}


def lane_task_keys(lane: str) -> set[str]:
    return {
        definition.task_key
        for definition in QUEUE_TASK_REGISTRY.values()
        if definition.lane == lane
    }


# default 道领取时排除的 key：专属道任务不允许被 default 道抢走。
NON_DEFAULT_LANE_TASK_KEYS: set[str] = {
    definition.task_key
    for definition in QUEUE_TASK_REGISTRY.values()
    if definition.lane != LANE_DEFAULT
}
