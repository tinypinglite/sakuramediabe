from datetime import datetime
from typing import TYPE_CHECKING, Dict, List, Optional

from pydantic import Field, computed_field

from src.schema.common.base import SchemaModel
from src.schema.catalog.actors import ImageResource
# 把下载任务的 import_status 原始码转换成中文展示文案，取值集中在 media_import_status 模块。
from src.common.media_import_status import describe_import_status

if TYPE_CHECKING:
    # 仅在类型检查时引入 Movie，避免 schema 层反向依赖 model 层。
    from src.model import Movie


class DownloadClientResource(SchemaModel):
    id: int
    name: str
    # 下载入口种类：qbittorrent / cloud115。qb 专属连接字段对 cloud115 kind 为 None。
    kind: str
    base_url: Optional[str] = None
    username: Optional[str] = None
    client_save_path: Optional[str] = None
    local_root_path: Optional[str] = None
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
                "kind": client.kind,
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
    # qbittorrent（默认）需提供全部 qb 连接字段；cloud115 只需 name + media_library_id
    #（凭据在 cloud115 媒体库的 backend_config 里，不在下载器上）。
    kind: str = "qbittorrent"
    base_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    client_save_path: Optional[str] = None
    local_root_path: Optional[str] = None
    media_library_id: int


class DownloadClientUpdateRequest(SchemaModel):
    # kind 不可更新：入口种类决定了字段语义，改种类应删除重建。
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
    # cloud115 kind 没有 base_url / 版本概念，相关字段为 None。
    base_url: Optional[str] = None
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


class DownloadCandidateClientResource(SchemaModel):
    """候选资源允许选择的下载器概要。"""

    id: int
    name: str
    kind: str


class DownloadCandidateResource(SchemaModel):
    source: str
    indexer_name: str
    indexer_kind: str
    # 按全局 kind 偏好预解析出的下载器；提交时用户可用 client_id 显式覆盖。
    resolved_client_id: int
    resolved_client_name: str
    resolved_client_kind: str
    # 当前索引器绑定的全部下载器；前端以 resolved_client_id 为默认选项。
    download_clients: List[DownloadCandidateClientResource]
    movie_number: str
    title: str
    size_bytes: int
    seeders: int
    magnet_url: str = ""
    torrent_url: str = ""
    # 种子身份，已规范化为 40 位小写 hex（``canonicalize_btih``）。取自 torznab 的 infohash
    # 属性，拿不到时从磁力链解析；两者都没有则为空串——**空串不代表"没有这个种子"，只代表
    # 本次没能廉价地确定它的身份**，选种黑名单据此跳过该候选，不要拿空串去做相等比较。
    info_hash: str = ""
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
    # 内联影片元数据：让前端下载卡片可以直接展示中文标题与封面缩略图，
    # 避免每张卡片再打一次 GET /movies/search/local 二次查。仅当 movie_number 命中本地
    # 影片库时才会有值；未入库的番号（比如 predownload）保持 None，前端做 placeholder 处理。
    movie_title: Optional[str] = None
    movie_cover: Optional[ImageResource] = None
    created_at: datetime
    updated_at: datetime

    @computed_field
    @property
    def import_status_label(self) -> str:
        # 下载任务导入阶段状态的中文说明。
        return describe_import_status(self.import_status)

    @classmethod
    def from_model(cls, task, *, movie: "Optional[Movie]" = None) -> "DownloadTaskResource":
        data = {
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
        if movie is not None:
            # 中文标题优先，回落到原始标题；两者都为空时保持 None（前端会 fallback 到 task.name）。
            title_zh = (movie.title_zh or "").strip()
            title = (movie.title or "").strip()
            data["movie_title"] = title_zh or title or None
            # cover_image 是可空 FK；有值才组装 ImageResource，让签名逻辑走 field_validator。
            if movie.cover_image is not None:
                data["movie_cover"] = ImageResource.from_attributes_model(movie.cover_image)
        return cls.model_validate(data)

    @classmethod
    def from_models(
        cls,
        tasks,
        *,
        movies_by_number: "Optional[Dict[str, Movie]]" = None,
    ) -> List["DownloadTaskResource"]:
        # movies_by_number 由调用方在 service 层批量查出（按 movie_number 索引），
        # 单次列表只做一趟 JOIN，避免 N+1。未传时退化为原有行为，兼容单任务/事件路径。
        index = movies_by_number or {}
        return [cls.from_model(task, movie=index.get(task.movie)) for task in tasks]


class DownloadTasksQuery(SchemaModel):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)
    client_id: Optional[int] = Field(default=None, gt=0)
    movie_number: Optional[str] = None
    # 按下载状态精确筛选（如 downloading / seeding / paused / failed）。取值集合与
    # ALLOWED_DOWNLOAD_STATES 一致；空串 / null 表示不过滤。
    download_state: Optional[str] = None
    sort: Optional[str] = None


class DownloadTaskActionResponse(SchemaModel):
    task_id: int
    action: str
    status: str = "ok"


class DownloadTaskProgressResource(SchemaModel):
    task_id: int
    client_id: int
    movie_number: Optional[str] = None
    name: str
    info_hash: str
    progress: float
    raw_state: str
    download_state: str
    download_speed_bytes: int
    uploaded_speed_bytes: int
    downloaded_bytes: int
    total_size_bytes: int
    eta_seconds: Optional[int] = None


class DownloadClientTransferResource(SchemaModel):
    client_id: int
    download_speed_bytes: int
    upload_speed_bytes: int
    connection_status: Optional[str] = None


class DownloadRequestCreateResponse(SchemaModel):
    task: DownloadTaskResource
    created: bool


class DownloadClientSyncResponse(SchemaModel):
    client_id: int
    scanned_count: int
    created_count: int
    updated_count: int
    unchanged_count: int
    # 本次同步中因 qB 侧已删除、本地也无 in-flight 导入而清理掉的幽灵任务数。
    removed_count: int = 0


class DownloadTaskImportResponse(SchemaModel):
    task_id: int
    import_job_id: int
    task_run_id: int
    status: str
