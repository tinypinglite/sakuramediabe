"""影片导入相关状态/结果的集中定义（transfers 域）。

放在 ``src/common`` 这个零依赖叶子层，是为了让 service 与 schema 两侧都能直接引用而不触发循环导入。
把原本散落在 transfers/common.py、media_import_service.py、media_import_job_service.py、
download_sync_service.py、download_task_service.py 中的下列取值统一收口到一处，并为每个值
附带中文解释，作为"落库/序列化原始字符串"与"API 展示文案"的唯一权威来源：

- ``DownloadTask.import_status``：下载任务的导入阶段状态
- ``ImportJob.state``：可视化导入作业的执行状态
- ``failed_files[].kind``：失败条目分类，决定该项是否可被重导/删除/重命名
- ``failed_files[].reason``：单条导入失败的具体原因

约定：
- 每个 ``*_*`` 常量的值即落库与序列化使用的原始字符串，不要直接写字面量，统一引用本模块常量。
- ``*_DESCRIPTIONS`` 字典给出"取值 -> 中文说明"的映射。
- ``describe_*`` 提供查询入口，未知取值回退原值，避免展示层因脏数据崩溃。

注意：catalog 域（``movie_service`` / ``actor_service``）的单片元数据抓取也用到了
``metadata_fetch_failed`` 等同名 reason 字符串，但那是另一套语义，不在本模块管辖范围内。
"""

from typing import Dict

# ===== DownloadTask.import_status：下载任务的导入阶段状态 =====
IMPORT_STATUS_PENDING = "pending"
IMPORT_STATUS_RUNNING = "running"
IMPORT_STATUS_COMPLETED = "completed"
IMPORT_STATUS_FAILED = "failed"
IMPORT_STATUS_SKIPPED = "skipped"

IMPORT_STATUS_DESCRIPTIONS: Dict[str, str] = {
    IMPORT_STATUS_PENDING: "待导入：下载已完成，等待自动导入触发",
    IMPORT_STATUS_RUNNING: "导入中：导入作业正在执行",
    IMPORT_STATUS_COMPLETED: "已导入：媒体文件全部成功入库",
    IMPORT_STATUS_FAILED: "导入失败：存在未成功导入的文件",
    IMPORT_STATUS_SKIPPED: "已跳过：任务未触发导入",
}
ALLOWED_IMPORT_STATUSES = set(IMPORT_STATUS_DESCRIPTIONS)


# ===== ImportJob.state：可视化导入作业的执行状态 =====
IMPORT_JOB_STATE_PENDING = "pending"
IMPORT_JOB_STATE_RUNNING = "running"
IMPORT_JOB_STATE_COMPLETED = "completed"
IMPORT_JOB_STATE_FAILED = "failed"

IMPORT_JOB_STATE_DESCRIPTIONS: Dict[str, str] = {
    IMPORT_JOB_STATE_PENDING: "待执行：作业已创建，尚未开始扫描导入",
    IMPORT_JOB_STATE_RUNNING: "执行中：正在扫描、抓取元数据或导入文件",
    IMPORT_JOB_STATE_COMPLETED: "已完成：本次导入无失败文件",
    IMPORT_JOB_STATE_FAILED: "已失败：存在单文件失败或作业级异常",
}
# 仅终态作业才允许执行失败文件的删除/重命名/重导，避免与运行中的导入线程竞争。
TERMINAL_JOB_STATES = (IMPORT_JOB_STATE_COMPLETED, IMPORT_JOB_STATE_FAILED)


# ===== failed_files[].kind：失败条目分类 =====
FAILED_FILE_KIND_FILE = "file"        # 单个媒体文件级失败，可重导/删除/重命名
FAILED_FILE_KIND_SKIPPED = "skipped"  # 主动跳过（如小文件），仅信息展示
FAILED_FILE_KIND_WARNING = "warning"  # 导入后告警（如删源失败/多字幕），不做文件级操作
FAILED_FILE_KIND_JOB = "job"          # 任务级失败，path 通常为目录，不做文件级操作

FAILED_FILE_KIND_DESCRIPTIONS: Dict[str, str] = {
    FAILED_FILE_KIND_FILE: "单文件失败：可重导/删除/重命名",
    FAILED_FILE_KIND_SKIPPED: "主动跳过：仅信息展示，无需处理",
    FAILED_FILE_KIND_WARNING: "导入后告警：媒体已入库，仅提示",
    FAILED_FILE_KIND_JOB: "任务级失败：作用对象通常为目录，不做文件级操作",
}


# ===== failed_files[].reason：单条导入失败的具体原因 =====
FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND = "movie_number_not_found"
FAILURE_REASON_METADATA_FETCH_FAILED = "metadata_fetch_failed"
FAILURE_REASON_IMAGE_DOWNLOAD_FAILED = "image_download_failed"
FAILURE_REASON_METADATA_UPSERT_FAILED = "metadata_upsert_failed"
FAILURE_REASON_MEDIA_IMPORT_FAILED = "media_import_failed"
FAILURE_REASON_VR_MEDIA_MERGE_FAILED = "vr_media_merge_failed"
FAILURE_REASON_FILE_TOO_SMALL = "file_too_small"
FAILURE_REASON_MERGE_SUBTITLE_SKIPPED_MULTIPLE_SIDECARS = "merge_subtitle_skipped_multiple_sidecars"
FAILURE_REASON_SOURCE_DELETE_FAILED = "source_delete_failed"
FAILURE_REASON_IMPORT_JOB_CRASHED = "import_job_crashed"
FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED = "import_job_bootstrap_failed"
FAILURE_REASON_IMPORT_JOB_INTERRUPTED = "import_job_interrupted"
FAILURE_REASON_RETRY_SOURCES_MISSING = "retry_sources_missing"

FAILURE_REASON_DESCRIPTIONS: Dict[str, str] = {
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND: "未识别番号：无法从文件名/路径解析出影片番号",
    FAILURE_REASON_METADATA_FETCH_FAILED: "元数据抓取失败：从站点获取影片信息失败",
    FAILURE_REASON_IMAGE_DOWNLOAD_FAILED: "图片下载失败：影片封面/海报下载失败",
    FAILURE_REASON_METADATA_UPSERT_FAILED: "元数据入库失败：影片信息写入数据库失败",
    FAILURE_REASON_MEDIA_IMPORT_FAILED: "文件导入失败：单个媒体文件搬运/落库异常",
    FAILURE_REASON_VR_MEDIA_MERGE_FAILED: "VR 合并失败：VR 影片多分段合并出错",
    FAILURE_REASON_FILE_TOO_SMALL: "文件过小：低于最小体积阈值，按样本/残片跳过",
    FAILURE_REASON_MERGE_SUBTITLE_SKIPPED_MULTIPLE_SIDECARS: "字幕未合并：VR 合并时发现多个外挂字幕，未自动合并",
    FAILURE_REASON_SOURCE_DELETE_FAILED: "源文件删除失败：媒体已入库，但清理源文件失败（仅告警）",
    FAILURE_REASON_IMPORT_JOB_CRASHED: "导入流程崩溃：导入过程整体异常中断",
    FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED: "作业启动失败：导入作业入队/引导阶段失败",
    FAILURE_REASON_IMPORT_JOB_INTERRUPTED: "导入进程中断：作业未正常结束（孤儿恢复判失败）",
    FAILURE_REASON_RETRY_SOURCES_MISSING: "源文件缺失：待重导的源文件均已不存在",
}

# 失败原因 -> 条目分类：决定该失败项是否可被用户重导/删除/重命名。
_FAILED_FILE_KIND_BY_REASON: Dict[str, str] = {
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_METADATA_FETCH_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_IMAGE_DOWNLOAD_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_METADATA_UPSERT_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_MEDIA_IMPORT_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_VR_MEDIA_MERGE_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_FILE_TOO_SMALL: FAILED_FILE_KIND_SKIPPED,
    FAILURE_REASON_MERGE_SUBTITLE_SKIPPED_MULTIPLE_SIDECARS: FAILED_FILE_KIND_WARNING,
    FAILURE_REASON_SOURCE_DELETE_FAILED: FAILED_FILE_KIND_WARNING,
    FAILURE_REASON_IMPORT_JOB_CRASHED: FAILED_FILE_KIND_JOB,
    FAILURE_REASON_IMPORT_JOB_BOOTSTRAP_FAILED: FAILED_FILE_KIND_JOB,
    FAILURE_REASON_IMPORT_JOB_INTERRUPTED: FAILED_FILE_KIND_JOB,
    FAILURE_REASON_RETRY_SOURCES_MISSING: FAILED_FILE_KIND_JOB,
}


def classify_failed_file_kind(reason: str) -> str:
    """按失败原因归类失败条目；未知原因按可操作的文件级失败处理。"""
    return _FAILED_FILE_KIND_BY_REASON.get(reason or "", FAILED_FILE_KIND_FILE)


def make_failure_item(path, reason: str, detail: str = "") -> Dict[str, str]:
    """构造带分类的失败条目，作为写入 ``failed_files`` 的唯一来源。"""
    return {
        "path": str(path),
        "reason": reason,
        "detail": detail,
        "kind": classify_failed_file_kind(reason),
    }


def describe_import_status(value: str) -> str:
    """返回 import_status 的中文说明；未知取值回退原值。"""
    return IMPORT_STATUS_DESCRIPTIONS.get(value or "", value or "")


def describe_import_job_state(value: str) -> str:
    """返回 ImportJob.state 的中文说明；未知取值回退原值。"""
    return IMPORT_JOB_STATE_DESCRIPTIONS.get(value or "", value or "")


def describe_failed_file_kind(value: str) -> str:
    """返回失败条目分类的中文说明；未知取值回退原值。"""
    return FAILED_FILE_KIND_DESCRIPTIONS.get(value or "", value or "")


def describe_failure_reason(value: str) -> str:
    """返回失败原因的中文说明；未知取值回退原值。"""
    return FAILURE_REASON_DESCRIPTIONS.get(value or "", value or "")
