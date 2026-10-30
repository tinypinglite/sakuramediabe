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


class DownloadSelectionRejectionResource(SchemaModel):
    code: str
    message: str
    group_label: str | None = None
    term: str | None = None
    field: str | None = None
    limit: int | None = None
    actual: int | str | None = None


class DownloadCandidateSelectionEvaluationResource(SchemaModel):
    eligible: bool
    rejection_reasons: list[DownloadSelectionRejectionResource] = Field(
        default_factory=list
    )
    matched_required_group_labels: list[str] = Field(default_factory=list)
    matched_preferred_group_label: str | None = None
    preference_rank: int | None = None
    selection_rank: int | None = None
    within_attempt_limit: bool = False


class DownloadCandidateResource(SchemaModel):
    source_uri: str
    indexer_name: str
    indexer_kind: str
    resolved_client_id: int
    resolved_client_name: str
    download_clients: list[DownloadCandidateClientResource]
    movie_number: str
    title: str
    # title 保留标题+描述的展示文本；规则匹配和原始标题展示使用这两个字段。
    release_title: str = ""
    description: str = ""
    published_at: datetime | None = None
    size_bytes: int
    seeders: int
    selection_evaluation: DownloadCandidateSelectionEvaluationResource | None = None


class DownloadCandidatesQuery(SchemaModel):
    movie_number: str
    indexer_kind: str | None = None


class DownloadResourceBlacklistQuery(SchemaModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)


class DownloadResourceBlacklistSubmissionResource(SchemaModel):
    movie_number: str
    indexer_name: str
    title: str
    client_id: int
    task_id: int | None = None
    submitted_at: datetime


class DownloadResourceBlacklistItemResource(SchemaModel):
    info_hash: str
    blacklisted_at: datetime
    latest_submission: DownloadResourceBlacklistSubmissionResource | None = None


class DownloadCandidateCreatePayload(SchemaModel):
    source_uri: str
    indexer_name: str
    title: str
    size_bytes: int
    seeders: int
    release_title: str = ""
    description: str = ""


class DownloadRequestCreateRequest(SchemaModel):
    client_id: int | None = Field(default=None, gt=0)
    movie_number: str
    candidate: DownloadCandidateCreatePayload


class DownloadManualRequest(SchemaModel):
    """用户手动提交磁力链；种子文件走 /download-requests/manual/torrent。"""

    client_id: int = Field(gt=0)
    media_kind: Literal["jav", "video"]
    source_uri: str = Field(min_length=1)
    movie_number: str | None = None
    collection_id: int | None = Field(default=None, gt=0)
    display_name: str | None = None
    delete_source_after_import: bool = False


class DownloadTaskResource(SchemaModel):
    id: int
    client_id: int
    movie_number: str | None = None
    media_kind: str = "jav"
    collection_id: int | None = None
    delete_source_after_import: bool = False
    display_name: str | None = None
    name: str
    remote_id: str
    state: str
    progress: float
    import_status: str
    movie_title: str | None = None
    movie_cover: ImageResource | None = None
    movie_thin_cover: ImageResource | None = None
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
            "media_kind": task.media_kind or "jav",
            "collection_id": task.collection_id,
            "delete_source_after_import": bool(task.delete_source_after_import),
            "display_name": task.display_name,
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
            if movie.thin_cover_image is not None:
                data["movie_thin_cover"] = ImageResource.from_attributes_model(movie.thin_cover_image)
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


class DownloadTaskBatchImportRequest(SchemaModel):
    task_ids: list[int] = Field(min_length=1, max_length=100)


class DownloadTaskBatchImportResponse(SchemaModel):
    accepted_count: int
    skipped_task_ids: list[int] = Field(default_factory=list)


class DownloadTaskFileResource(SchemaModel):
    """下载任务源内的单个文件；宿主内部 source_ref 不对外暴露。"""

    name: str
    relative_path: str
    size_bytes: int
    is_video: bool
