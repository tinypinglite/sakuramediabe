from datetime import datetime
from typing import List, Optional

from pydantic import computed_field

from src.schema.common.base import SchemaModel
# 把下载任务的 import_status 原始码转换成中文展示文案，取值集中在 media_import_status 模块。
from src.common.media_import_status import describe_import_status


class DownloadClientResource(SchemaModel):
    id: int
    name: str
    base_url: str
    username: str
    client_save_path: str
    local_root_path: str
    media_library_id: int
    has_password: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, client) -> "DownloadClientResource":
        return cls.model_validate(
            {
                "id": client.id,
                "name": client.name,
                "base_url": client.base_url,
                "username": client.username,
                "client_save_path": client.client_save_path,
                "local_root_path": client.local_root_path,
                "media_library_id": client.media_library_id,
                "has_password": bool((client.password or "").strip()),
                "created_at": client.created_at,
                "updated_at": client.updated_at,
            }
        )

    @classmethod
    def from_models(cls, clients) -> List["DownloadClientResource"]:
        return [cls.from_model(client) for client in clients]


class DownloadClientCreateRequest(SchemaModel):
    name: str
    base_url: str
    username: str
    password: str
    client_save_path: str
    local_root_path: str
    media_library_id: int


class DownloadClientUpdateRequest(SchemaModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    client_save_path: Optional[str] = None
    local_root_path: Optional[str] = None
    media_library_id: Optional[int] = None


class DownloadClientTestError(SchemaModel):
    type: str
    message: str


class DownloadClientTestResponse(SchemaModel):
    healthy: bool
    checked_at: datetime
    client_id: int
    client_name: str
    base_url: str
    elapsed_ms: int
    version: Optional[str] = None
    web_api_version: Optional[str] = None
    error: Optional[DownloadClientTestError] = None


class DownloadClientStorageTestError(SchemaModel):
    type: str
    message: str


class DownloadClientStorageDirectoryMappingResult(SchemaModel):
    status: str
    client_save_path: str
    local_root_path: str
    probe_remote_dir: str
    probe_local_dir: str
    sentinel_visible_to_qb: bool
    error: Optional[DownloadClientStorageTestError] = None


class DownloadClientStorageHardlinkResult(SchemaModel):
    status: str
    supported: bool
    source_path: str
    target_path: str
    error: Optional[DownloadClientStorageTestError] = None


class DownloadClientStorageTestResponse(SchemaModel):
    healthy: bool
    checked_at: datetime
    client_id: int
    client_name: str
    elapsed_ms: int
    warnings: List[str] = []
    directory_mapping: DownloadClientStorageDirectoryMappingResult
    hardlink: DownloadClientStorageHardlinkResult


class DownloadClientProbeTestRequest(SchemaModel):
    """连通性预检 payload。

    - `password` 可为空/缺省;此时必须提供 `client_id`,后端从 DB 合并原密码
      (对齐"编辑时密码留空=不改"约定)。
    - `client_id` 仅用于取原密码;probe 端点不会落库。
    """

    base_url: str
    username: str
    password: Optional[str] = None
    client_id: Optional[int] = None


class DownloadClientProbeStorageTestRequest(SchemaModel):
    """目录映射 + 硬链接预检 payload。

    - `password` 处理规则同 `DownloadClientProbeTestRequest`。
    - `media_library_id` 决定硬链接目标根路径 (probe 端点不会落库)。
    """

    base_url: str
    username: str
    password: Optional[str] = None
    client_save_path: str
    local_root_path: str
    media_library_id: int
    client_id: Optional[int] = None


class DownloadCandidateResource(SchemaModel):
    source: str
    indexer_name: str
    indexer_kind: str
    resolved_client_id: int
    resolved_client_name: str
    movie_number: str
    title: str
    size_bytes: int
    seeders: int
    magnet_url: str = ""
    torrent_url: str = ""
    tags: List[str] = []


class DownloadCandidatesQuery(SchemaModel):
    movie_number: str
    indexer_kind: Optional[str] = None


class DownloadCandidateCreatePayload(SchemaModel):
    source: str
    indexer_name: str
    indexer_kind: str
    title: str
    size_bytes: int
    seeders: int
    magnet_url: str = ""
    torrent_url: str = ""
    tags: List[str] = []


class DownloadRequestCreateRequest(SchemaModel):
    client_id: Optional[int] = None
    movie_number: str
    candidate: DownloadCandidateCreatePayload


class DownloadTaskResource(SchemaModel):
    id: int
    client_id: int
    movie_number: Optional[str] = None
    name: str
    info_hash: str
    save_path: str
    progress: float
    download_state: str
    import_status: str
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def import_status_label(self) -> str:
        # 下载任务导入阶段状态的中文说明。
        return describe_import_status(self.import_status)

    @classmethod
    def from_model(cls, task) -> "DownloadTaskResource":
        return cls.model_validate(
            {
                "id": task.id,
                "client_id": task.client_id,
                "movie_number": task.movie,
                "name": task.name,
                "info_hash": task.info_hash,
                "save_path": task.save_path,
                "progress": task.progress,
                "download_state": task.download_state,
                "import_status": task.import_status,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
        )

    @classmethod
    def from_models(cls, tasks) -> List["DownloadTaskResource"]:
        return [cls.from_model(task) for task in tasks]


class DownloadRequestCreateResponse(SchemaModel):
    task: DownloadTaskResource
    created: bool


class DownloadClientSyncResponse(SchemaModel):
    client_id: int
    scanned_count: int
    created_count: int
    updated_count: int
    unchanged_count: int


class DownloadTaskImportResponse(SchemaModel):
    task_id: int
    import_job_id: int
    task_run_id: int
    status: str
