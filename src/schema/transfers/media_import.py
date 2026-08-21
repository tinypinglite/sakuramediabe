"""统一媒体导入协议。"""

from typing import Literal

from pydantic import Field, model_validator

from src.schema.common.base import SchemaModel


class FilesystemEntryResource(SchemaModel):
    name: str
    path: str
    type: Literal["dir", "video", "file"]
    size: int
    is_video: bool


class FilesystemListResponse(SchemaModel):
    path: str
    parent: str | None = None
    entries: list[FilesystemEntryResource]


class ImportRequest(SchemaModel):
    """JAV / 普通视频、本地 / 115 共用的一次完整导入请求。"""

    media_kind: Literal["jav", "video"]
    backend: Literal["local", "cloud115"]
    library_id: int
    source_path: str | None = None
    source_cid: str | None = None
    source_fid: str | None = None
    transfer_mode: Literal["auto", "cleanup-source"] | None = None
    collection_id: int | None = None

    @model_validator(mode="after")
    def validate_combination(self) -> "ImportRequest":
        sources = (self.source_path, self.source_cid, self.source_fid)
        if sum(value is not None for value in sources) != 1:
            raise ValueError("exactly one import source is required")
        if self.backend == "local" and self.source_path is None:
            raise ValueError("local import requires source_path")
        if self.backend == "cloud115" and self.source_path is not None:
            raise ValueError("cloud115 import requires source_cid or source_fid")
        if self.media_kind == "jav" and self.source_fid is not None:
            raise ValueError("jav import does not support source_fid")
        if self.media_kind == "jav" and self.collection_id is not None:
            raise ValueError("jav import does not support collection_id")
        if self.backend == "cloud115" and self.transfer_mode not in (
            None,
            "cleanup-source",
        ):
            raise ValueError("cloud115 import is move-only")
        return self


class ImportResult(SchemaModel):
    imported_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    new_playable_movies: list[dict[str, object]] = Field(default_factory=list)
    created_video_ids: list[int] = Field(default_factory=list)


class ImportAcceptedResponse(SchemaModel):
    task_run_id: int
    task_key: str
    state: str
