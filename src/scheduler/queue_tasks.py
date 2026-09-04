"""持久任务队列的参数化执行注册表。

不走 cron 的执行链路（导入族 / 单资源手动任务）由 producer 入队，
worker 按同一个 ``JobDefinition`` 解析执行。handler 签名
``(reporter, params) -> dict``，抛异常即任务失败（领域侧终态由 handler 自己收口后
re-raise）。

lane 划分并发道：``import`` 道复刻旧 ``DownloadImportRunner``（2 并发），
其余任务使用 default（4 并发），导入使用 import（2 并发）。
"""

from __future__ import annotations

from typing import Any

from src.scheduler.contracts import JobDefinition

LANE_DEFAULT = "default"
LANE_IMPORT = "import"
LANE_TRANSFER = "transfer"

# 并发道容量：default 复刻 APS ThreadPoolExecutor(4)，导入道使用 2 并发。
LANE_CONCURRENCY: dict[str, int] = {
    LANE_DEFAULT: 4,
    LANE_IMPORT: 2,
    LANE_TRANSFER: 1,
}


def _run_library_import(reporter, params: dict[str, Any]) -> dict:
    from src.service.transfers.shared.import_task_service import ImportTaskService

    return ImportTaskService.execute(reporter, params)


def _run_media_storage_transfer(reporter, params: dict[str, Any]) -> dict:
    from src.service.transfers.shared.media_transfer_task_service import (
        MediaTransferTaskService,
    )

    return MediaTransferTaskService.execute(reporter, params)


def _recover_media_storage_transfers() -> dict[str, int]:
    from src.service.transfers.shared.media_transfer_task_service import (
        MediaTransferTaskService,
    )

    return MediaTransferTaskService.recover_interrupted_transfers()


def _run_gfriends_filetree_refresh(_reporter, params: dict[str, Any]) -> dict:
    from src.metadata.factory import refresh_gfriends_filetree

    # cron 的 NULL 参数表示强制刷新；启动预热显式传 false。
    force = params.get("force", True)
    if not isinstance(force, bool):
        raise TypeError("gfriends_filetree_refresh.force 必须是布尔值")
    return refresh_gfriends_filetree(force=force)


QUEUE_TASK_REGISTRY: dict[str, JobDefinition] = {
    definition.task_key: definition
    for definition in (
        JobDefinition(
            task_key="library_import",
            log_name="library-import",
            cli_name="library-import",
            cli_help="执行一次媒体库导入",
            manual_only=True,
            handler=_run_library_import,
            lane=LANE_IMPORT,
        ),
        JobDefinition(
            task_key="media_storage_transfer",
            log_name="media-storage-transfer",
            cli_name="media-storage-transfer",
            cli_help="执行一次媒体存储迁移",
            manual_only=True,
            handler=_run_media_storage_transfer,
            business_recovery=_recover_media_storage_transfers,
            lane=LANE_TRANSFER,
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
