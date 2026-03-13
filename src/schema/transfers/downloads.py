from datetime import datetime
from typing import List, Optional

from src.schema.common.base import SchemaModel


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
    status: str
