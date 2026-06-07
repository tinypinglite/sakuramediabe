"""media_import_status 集中模块的单元测试。

锁定导入状态/失败原因的取值集合、失败分类映射、中文说明完整性，以及 describe_* 的未知值回退，
避免后续改动悄悄漏配中文或破坏落库字符串。
"""

from src.common import media_import_status as mis


def test_import_status_values_and_descriptions_aligned():
    # 允许值集合应与中文说明字典严格一致，避免出现"有取值无说明"或反之。
    assert mis.ALLOWED_IMPORT_STATUSES == set(mis.IMPORT_STATUS_DESCRIPTIONS)
    assert mis.ALLOWED_IMPORT_STATUSES == {
        mis.IMPORT_STATUS_PENDING,
        mis.IMPORT_STATUS_RUNNING,
        mis.IMPORT_STATUS_COMPLETED,
        mis.IMPORT_STATUS_FAILED,
        mis.IMPORT_STATUS_SKIPPED,
    }


def test_terminal_job_states_are_completed_and_failed():
    assert mis.TERMINAL_JOB_STATES == (
        mis.IMPORT_JOB_STATE_COMPLETED,
        mis.IMPORT_JOB_STATE_FAILED,
    )
    # 终态必须都在 state 说明字典里。
    for state in mis.TERMINAL_JOB_STATES:
        assert state in mis.IMPORT_JOB_STATE_DESCRIPTIONS


def test_every_failure_reason_has_kind_and_description():
    # 每个失败原因都必须有分类与中文说明，且分类落在四种已知 kind 内。
    valid_kinds = {
        mis.FAILED_FILE_KIND_FILE,
        mis.FAILED_FILE_KIND_SKIPPED,
        mis.FAILED_FILE_KIND_WARNING,
        mis.FAILED_FILE_KIND_JOB,
    }
    reasons = set(mis.FAILURE_REASON_DESCRIPTIONS)
    assert reasons == set(mis._FAILED_FILE_KIND_BY_REASON)
    for reason, kind in mis._FAILED_FILE_KIND_BY_REASON.items():
        assert kind in valid_kinds
        assert mis.FAILURE_REASON_DESCRIPTIONS[reason]


def test_classify_failed_file_kind_known_and_unknown():
    assert mis.classify_failed_file_kind(mis.FAILURE_REASON_FILE_TOO_SMALL) == mis.FAILED_FILE_KIND_SKIPPED
    assert mis.classify_failed_file_kind(mis.FAILURE_REASON_SOURCE_DELETE_FAILED) == mis.FAILED_FILE_KIND_WARNING
    assert mis.classify_failed_file_kind(mis.FAILURE_REASON_IMPORT_JOB_CRASHED) == mis.FAILED_FILE_KIND_JOB
    # 未知原因按可操作的文件级失败兜底。
    assert mis.classify_failed_file_kind("brand_new_reason") == mis.FAILED_FILE_KIND_FILE
    assert mis.classify_failed_file_kind("") == mis.FAILED_FILE_KIND_FILE


def test_make_failure_item_shape():
    item = mis.make_failure_item("/data/incoming/ABP-123.mp4", mis.FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND, "no number")
    assert item == {
        "path": "/data/incoming/ABP-123.mp4",
        "reason": "movie_number_not_found",
        "detail": "no number",
        "kind": "file",
    }


def test_describe_helpers_fallback_to_raw_value():
    assert mis.describe_import_status(mis.IMPORT_STATUS_PENDING) == "待导入：下载已完成，等待自动导入触发"
    assert mis.describe_import_job_state(mis.IMPORT_JOB_STATE_FAILED) == "已失败：存在单文件失败或作业级异常"
    assert mis.describe_failed_file_kind(mis.FAILED_FILE_KIND_WARNING) == "导入后告警：媒体已入库，仅提示"
    assert mis.describe_failure_reason(mis.FAILURE_REASON_RETRY_SOURCES_MISSING) == "源文件缺失：待重导的源文件均已不存在"
    # 未知/空取值回退原值，保证展示层不崩。
    assert mis.describe_failure_reason("unknown_reason") == "unknown_reason"
    assert mis.describe_import_status("") == ""
