"""导入作业状态写回的共享工具。

多个导入域（媒体导入 / 字幕导入）共用同一套终态语义：存在单文件失败或任务级失败
才判 failed，跳过项只进统计与失败列表、不影响终态。
"""

from __future__ import annotations

import json

from src.common.media_import_status import (
    IMPORT_JOB_STATE_COMPLETED,
    IMPORT_JOB_STATE_FAILED,
)
from src.common.runtime_time import utc_now_for_db


def finalize_import_job(
    job,
    *,
    imported_count: int,
    skipped_count: int,
    failed_count: int,
    failure_items: list[dict],
) -> None:
    """写回作业统计与终态；``failed_count > 0`` 判 failed，否则 completed。"""
    job.imported_count = imported_count
    job.skipped_count = skipped_count
    job.failed_count = failed_count
    job.failed_files = json.dumps(failure_items, ensure_ascii=False)
    job.state = (
        IMPORT_JOB_STATE_FAILED if failed_count > 0 else IMPORT_JOB_STATE_COMPLETED
    )
    job.finished_at = utc_now_for_db()
    job.save()
