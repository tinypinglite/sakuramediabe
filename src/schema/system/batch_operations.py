"""用户批量操作任务的请求与响应模型。"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator, model_validator

from src.schema.common.base import SchemaModel


class BatchOperationType(str, Enum):
    MEDIA_DELETE = "media_delete"
    CLIP_DELETE = "clip_delete"
    MEDIA_POINT_DELETE = "media_point_delete"
    VIDEO_DELETE = "video_delete"
    CLIP_COLLECTION_ADD = "clip_collection_add"
    CLIP_COLLECTION_REMOVE = "clip_collection_remove"
    MOMENT_COLLECTION_ADD = "moment_collection_add"
    MOMENT_COLLECTION_REMOVE = "moment_collection_remove"
    VIDEO_COLLECTION_ADD = "video_collection_add"
    VIDEO_COLLECTION_REMOVE = "video_collection_remove"
    DOWNLOAD_TASK_DELETE = "download_task_delete"


COLLECTION_OPERATIONS = frozenset(
    {
        BatchOperationType.CLIP_COLLECTION_ADD,
        BatchOperationType.CLIP_COLLECTION_REMOVE,
        BatchOperationType.MOMENT_COLLECTION_ADD,
        BatchOperationType.MOMENT_COLLECTION_REMOVE,
        BatchOperationType.VIDEO_COLLECTION_ADD,
        BatchOperationType.VIDEO_COLLECTION_REMOVE,
    }
)


class BatchOperationRequest(SchemaModel):
    operation: BatchOperationType
    # 各操作的 ids 语义：媒体/切片/时刻/视频/下载任务的单实体 id；
    # 合集成员操作分别为 clip_id / point_id / video_item_id（remove 为成员 item_id）。
    ids: list[int] = Field(min_length=1, max_length=1000)
    target_collection_id: int | None = Field(default=None, gt=0)
    delete_files: bool = False

    @field_validator("ids", mode="before")
    @classmethod
    def reject_boolean_ids(cls, value):
        if isinstance(value, (list, tuple)) and any(
            isinstance(item, bool) for item in value
        ):
            raise ValueError("ids 必须全部为正整数")
        return value

    @field_validator("ids")
    @classmethod
    def validate_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("ids 必须全部为正整数")
        if len(set(value)) != len(value):
            raise ValueError("ids 不可重复")
        return value

    @model_validator(mode="after")
    def validate_operation_params(self):
        needs_collection = self.operation in COLLECTION_OPERATIONS
        if needs_collection and self.target_collection_id is None:
            raise ValueError("该操作必须提供 target_collection_id")
        if not needs_collection and self.target_collection_id is not None:
            raise ValueError("该操作不接受 target_collection_id")
        if self.delete_files and self.operation is not BatchOperationType.DOWNLOAD_TASK_DELETE:
            raise ValueError("delete_files 仅用于下载任务删除")
        return self


class BatchOperationAcceptedResponse(SchemaModel):
    task_run_id: int
    task_key: str
    state: str
