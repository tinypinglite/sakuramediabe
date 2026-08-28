from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, computed_field

from src.common.media_import_status import describe_import_status
from src.schema.catalog.actors import ImageResource
from src.schema.common.base import SchemaModel

if TYPE_CHECKING:
    from src.model import Movie


class DownloadClientResource(SchemaModel):
    id: int
    name: str
    library_id: int
    provider_config: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, client) -> "DownloadClientResource":
        return cls.model_validate(
            {
                "id": client.id,
                "name": client.name,
                "library_id": client.library_id,
                "provider_config": client.provider_config or {},
                "created_at": client.created_at,
                "updated_at": client.updated_at,
            }
        )

    @classmethod
    def from_models(cls, clients) -> list["DownloadClientResource"]:
        return [cls.from_model(client) for client in clients]


class DownloadClientCreateRequest(SchemaModel):
    name: str
    library_id: int = Field(gt=0)
    provider_config: dict[str, Any] = Field(default_factory=dict)


class DownloadClientUpdateRequest(SchemaModel):
    name: str | None = None
    library_id: int | None = Field(default=None, gt=0)
    provider_config: dict[str, Any] | None = None


class DownloadClientTestRequest(SchemaModel):
    library_id: int = Field(gt=0)
    provider_config: dict[str, Any] = Field(default_factory=dict)
    client_id: int | None = Field(default=None, gt=0)


class DownloadClientDiagnosticCheckResource(SchemaModel):
    key: str
    status: Literal["ok", "warning", "failed", "skipped"]
    code: str
    message: str
    details: dict[str, Any] | None = None


class DownloadClientDiagnosticResource(SchemaModel):
    status: Literal["ok", "warning", "failed"]
    checks: list[DownloadClientDiagnosticCheckResource]
    checked_at: datetime
    elapsed_ms: int


class DownloadCandidateClientResource(SchemaModel):
    """候选资源允许选择的下载器概要。"""

    id: int
    name: str


class DownloadCandidateResource(SchemaModel):
    source_uri: str
    indexer_name: str
    indexer_kind: str
    resolved_client_id: int
    resolved_client_name: str
    download_clients: list[DownloadCandidateClientResource]
    movie_number: str
    title: str
    size_bytes: int
    seeders: int


class DownloadCandidatesQuery(SchemaModel):
    movie_number: str
    indexer_kind: str | None = None


class DownloadCandidateCreatePayload(SchemaModel):
    source_uri: str
    indexer_name: str
    title: str
    size_bytes: int
    seeders: int


class DownloadRequestCreateRequest(SchemaModel):
    client_id: int | None = Field(default=None, gt=0)
    movie_number: str
    candidate: DownloadCandidateCreatePayload


class DownloadTaskResource(SchemaModel):
    id: int
    client_id: int
    movie_number: str | None = None
    name: str
    remote_id: str
    state: str
    progress: float
    import_status: str
    movie_title: str | None = None
    movie_cover: ImageResource | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def import_status_label(self) -> str:
        return describe_import_status(self.import_status)

    @classmethod
    def from_model(cls, task, *, movie: "Movie | None" = None) -> "DownloadTaskResource":
        data = {
            "id": task.id,
            "client_id": task.client_id,
            "movie_number": task.movie,
            "name": task.name,
            "remote_id": task.remote_id,
            "state": task.state,
            "progress": task.progress,
            "import_status": task.import_status,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }
        if movie is not None:
            title = (movie.title or "").strip()
            data["movie_title"] = title or None
            if movie.cover_image is not None:
                data["movie_cover"] = ImageResource.from_attributes_model(movie.cover_image)
        return cls.model_validate(data)

    @classmethod
    def from_models(
        cls,
        tasks,
        *,
        movies_by_number: "dict[str, Movie] | None" = None,
    ) -> list["DownloadTaskResource"]:
        index = movies_by_number or {}
        return [cls.from_model(task, movie=index.get(task.movie)) for task in tasks]


class DownloadTasksQuery(SchemaModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    client_id: int | None = Field(default=None, gt=0)
    movie_number: str | None = None
    sort: str | None = None


class DownloadRequestCreateResponse(SchemaModel):
    task: DownloadTaskResource
    created: bool


class DownloadClientSyncResponse(SchemaModel):
    client_id: int
    scanned_count: int
    created_count: int
    updated_count: int
    unchanged_count: int
    removed_count: int = 0


class DownloadTaskImportResponse(SchemaModel):
    task_id: int
    task_run_id: int
    status: str
