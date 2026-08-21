"""持久任务队列的参数化执行注册表。

不走 cron 的执行链路（导入族 / 秒传 / 单资源手动任务）以及需要显式参数覆写的
宿主启动任务，由 producer 入队、worker 按本表解析执行。handler 签名
``(reporter, params) -> dict``，运行在 worker 的 run_task 内，抛异常即任务失败
（领域侧终态由 handler 自己收口后 re-raise）。

lane 划分并发道：``import`` 道复刻旧 ``DownloadImportRunner``（2 并发），
``rapid_upload`` 道复刻旧 ``MediaRapidUploadRunner``（2 并发），其余 default（4 并发）。
与 JOB_REGISTRY 同 key 的条目只在运行带 params 时生效，否则按 cron 的无参 factory
解析；这同时覆盖单资源手动任务和 startup 参数差异。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


def _run_library_import(reporter, params: dict) -> dict:
    from src.service.transfers.shared.import_task_service import ImportTaskService

    return ImportTaskService.execute(reporter, params)


def _run_subtitle_directory_import(reporter, params: dict) -> dict:
    from src.service.catalog.subtitle_import_service import SubtitleImportService

    source_path = params.get("source_path")
    if not isinstance(source_path, str) or not source_path.strip():
        raise ValueError("subtitle_import_source_path_missing")
    # TaskRun params 是唯一执行凭据；不再反查已退役的字幕作业表。
    return SubtitleImportService().import_subtitles_from_source(
        source_path,
        progress_callback=reporter.progress_callback,
    )


def _run_media_rapid_upload(reporter, params: dict) -> dict:
    # 必须走 facade：executor 内部依赖 state_machine / item_executor 的方法，
    # 只有多继承组合后的 MediaRapidUploadService 才解析得到。
    from src.service.transfers.rapid_upload.facade import MediaRapidUploadService

    return MediaRapidUploadService.execute_batch_from_queue(reporter, params)


def _run_gfriends_filetree_refresh(_reporter, params: dict) -> dict:
    from src.metadata.factory import refresh_gfriends_filetree

    force = params.get("force")
    if not isinstance(force, bool):
        raise TypeError("gfriends_filetree_refresh.force 必须是布尔值")
    return refresh_gfriends_filetree(force=force)


QUEUE_TASK_REGISTRY: dict[str, QueueTaskDefinition] = {
    definition.task_key: definition
    for definition in (
        QueueTaskDefinition(
            task_key="library_import",
            log_name="library-import",
            handler=_run_library_import,
            lane=LANE_IMPORT,
        ),
        QueueTaskDefinition(
            task_key="subtitle_directory_import",
            log_name="subtitle-directory-import",
            handler=_run_subtitle_directory_import,
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
        # cron 的 NULL 参数仍走 JobDefinition factory（force=True）；只有启动预热的
        # 显式参数命中该宿主私有 override，从而保留 force=False 的缓存语义。
        QueueTaskDefinition(
            task_key="gfriends_filetree_refresh",
            log_name="gfriends-filetree-refresh",
            handler=_run_gfriends_filetree_refresh,
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
