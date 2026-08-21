"""影片导入相关状态/结果的集中定义（transfers 域）。

放在 ``src/common`` 这个零依赖叶子层，是为了让 service 与 schema 两侧都能直接引用而不触发循环导入。
把导入执行器共用的状态和失败原因收口到一处：

- ``DownloadTask.import_status``：下载任务的导入阶段状态
- 单文件处理结果的分类与原因

约定：
- 每个 ``*_*`` 常量的值即落库与序列化使用的原始字符串，不要直接写字面量，统一引用本模块常量。

注意：catalog 域（``movie_service`` / ``actor_service``）的单片元数据抓取也用到了
``metadata_fetch_failed`` 等同名 reason 字符串，但那是另一套语义，不在本模块管辖范围内。
"""


# ===== DownloadTask.import_status：下载任务的导入阶段状态 =====
IMPORT_STATUS_PENDING = "pending"
IMPORT_STATUS_RUNNING = "running"
IMPORT_STATUS_COMPLETED = "completed"
IMPORT_STATUS_FAILED = "failed"
IMPORT_STATUS_SKIPPED = "skipped"

IMPORT_STATUS_DESCRIPTIONS: dict[str, str] = {
    IMPORT_STATUS_PENDING: "待导入：下载已完成，等待自动导入触发",
    IMPORT_STATUS_RUNNING: "导入中：导入作业正在执行",
    IMPORT_STATUS_COMPLETED: "已导入：媒体文件全部成功入库",
    IMPORT_STATUS_FAILED: "导入失败：存在未成功导入的文件",
    IMPORT_STATUS_SKIPPED: "已跳过：任务未触发导入",
}
# 导入"还在途"的两个取值：等自动导入排队、或导入作业正在跑。其余取值（completed / failed /
# skipped）都表示这一趟导入已经跑完、不会再自动推进。中断恢复链路（DownloadSyncService 与
# Cloud115OfflineSyncService 的孤儿回收）把中断的任务重置回 pending，所以"在途"恒由这两个值表达。
UNFINISHED_IMPORT_STATUSES = (IMPORT_STATUS_PENDING, IMPORT_STATUS_RUNNING)


# ===== failed_files[].kind：失败条目分类 =====
FAILED_FILE_KIND_FILE = "file"        # 单个媒体文件级失败，可重导/删除/重命名
FAILED_FILE_KIND_SKIPPED = "skipped"  # 主动跳过（如小文件），仅信息展示
FAILED_FILE_KIND_WARNING = "warning"  # 导入后告警（如删源失败/多字幕），不做文件级操作
FAILED_FILE_KIND_JOB = "job"          # 任务级失败，path 通常为目录，不做文件级操作

# ===== failed_files[].reason：单条导入失败的具体原因 =====
FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND = "movie_number_not_found"
FAILURE_REASON_METADATA_FETCH_FAILED = "metadata_fetch_failed"
FAILURE_REASON_IMAGE_DOWNLOAD_FAILED = "image_download_failed"
FAILURE_REASON_METADATA_UPSERT_FAILED = "metadata_upsert_failed"
FAILURE_REASON_MEDIA_IMPORT_FAILED = "media_import_failed"
FAILURE_REASON_FILE_TOO_SMALL = "file_too_small"
FAILURE_REASON_SOURCE_DELETE_FAILED = "source_delete_failed"
FAILURE_REASON_NO_MEDIA_FILES_FOUND = "no_media_files_found"
FAILURE_REASON_ALREADY_INDEXED_PATH = "already_indexed_path"
FAILURE_REASON_DUPLICATE_FINGERPRINT = "duplicate_fingerprint"
FAILURE_REASON_CLOUD115_FILE_CENSORED = "cloud115_file_censored"
FAILURE_REASON_CLOUD115_TRANSFER_FAILED = "cloud115_transfer_failed"
FAILURE_REASON_CLOUD115_RENAME_FAILED = "cloud115_rename_failed"
FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED = "cloud115_metadata_probe_failed"
FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED = "cloud115_subtitle_download_failed"
FAILURE_REASON_SUBTITLE_MOVIE_NOT_FOUND = "subtitle_movie_not_found"
FAILURE_REASON_SUBTITLE_IMPORT_FAILED = "subtitle_import_failed"

# 失败原因 -> 条目分类：决定该失败项是否可被用户重导/删除/重命名。
_FAILED_FILE_KIND_BY_REASON: dict[str, str] = {
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_METADATA_FETCH_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_IMAGE_DOWNLOAD_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_METADATA_UPSERT_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_MEDIA_IMPORT_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_FILE_TOO_SMALL: FAILED_FILE_KIND_SKIPPED,
    FAILURE_REASON_SOURCE_DELETE_FAILED: FAILED_FILE_KIND_WARNING,
    # 已废弃的历史 reason 显式锁死分类：merge_subtitle 老告警行对应的媒体已入库，
    # 若掉到默认 file 会被前端当作可删除项暴露，误操作会删掉已入库媒体的源文件。
    "multi_part_merge_failed": FAILED_FILE_KIND_FILE,
    "merge_subtitle_skipped_multiple_sidecars": FAILED_FILE_KIND_WARNING,
    FAILURE_REASON_NO_MEDIA_FILES_FOUND: FAILED_FILE_KIND_JOB,
    FAILURE_REASON_ALREADY_INDEXED_PATH: FAILED_FILE_KIND_SKIPPED,
    FAILURE_REASON_DUPLICATE_FINGERPRINT: FAILED_FILE_KIND_SKIPPED,
    # cloud115 封禁文件已按 invalid 登记，重导没有意义，仅告警展示。
    FAILURE_REASON_CLOUD115_FILE_CENSORED: FAILED_FILE_KIND_WARNING,
    FAILURE_REASON_CLOUD115_TRANSFER_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_CLOUD115_RENAME_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_CLOUD115_METADATA_PROBE_FAILED: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_CLOUD115_SUBTITLE_DOWNLOAD_FAILED: FAILED_FILE_KIND_WARNING,
    FAILURE_REASON_SUBTITLE_MOVIE_NOT_FOUND: FAILED_FILE_KIND_FILE,
    FAILURE_REASON_SUBTITLE_IMPORT_FAILED: FAILED_FILE_KIND_FILE,
}


def classify_failed_file_kind(reason: str) -> str:
    """按失败原因归类失败条目；未知原因按可操作的文件级失败处理。"""
    return _FAILED_FILE_KIND_BY_REASON.get(reason or "", FAILED_FILE_KIND_FILE)


def make_failure_item(path, reason: str, detail: str = "") -> dict[str, str]:
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
