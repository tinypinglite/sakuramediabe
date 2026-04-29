from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from peewee import IntegrityError, fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import resolve_sort, validate_page
from src.common.runtime_time import utc_now_for_db
from src.model import ResourceTaskState
from src.model.base import get_database
from src.schema.common.pagination import PageResponse
from src.schema.system.resource_task_state import (
    ResourceTaskDefinitionResource,
    ResourceTaskRecordResource,
    TaskRecordStateCountsResource,
)
from src.service.system.activity_service import ActivityService
from src.service.system.resource_task_resolvers import (
    MEDIA_TASK_RECORD_RESOLVER,
    MOVIE_TASK_RECORD_RESOLVER,
    ResourceTaskRecordResolver,
)


@dataclass(frozen=True)
class ResourceTaskDefinition:
    task_key: str
    resource_type: str
    display_name: str
    default_sort: str
    allow_reset: bool = True
    resource_resolver: ResourceTaskRecordResolver | None = None


@dataclass(frozen=True)
class ResourceTaskStateSnapshot:
    task_key: str
    resource_type: str
    resource_id: int
    state: str
    attempt_count: int = 0
    last_attempted_at: datetime | None = None
    last_succeeded_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    last_task_run_id: int | None = None
    last_trigger_type: str | None = None
    extra: dict | list | None = None


class ResourceTaskStateService:
    STATE_PENDING = "pending"
    STATE_RUNNING = "running"
    STATE_SUCCEEDED = "succeeded"
    STATE_FAILED = "failed"
    TERMINAL_TRUE_MARKERS = ('"terminal": true', '"terminal":true')
    TASK_STATE_SORT_FIELDS = {
        "last_attempted_at:desc": (ResourceTaskState.last_attempted_at.desc(), ResourceTaskState.id.desc()),
        "last_attempted_at:asc": (ResourceTaskState.last_attempted_at.asc(), ResourceTaskState.id.asc()),
        "last_error_at:desc": (ResourceTaskState.last_error_at.desc(), ResourceTaskState.id.desc()),
        "attempt_count:desc": (ResourceTaskState.attempt_count.desc(), ResourceTaskState.id.desc()),
        "updated_at:desc": (ResourceTaskState.updated_at.desc(), ResourceTaskState.id.desc()),
        "updated_at:asc": (ResourceTaskState.updated_at.asc(), ResourceTaskState.id.asc()),
    }
    TASK_REGISTRY = {
        "movie_desc_sync": ResourceTaskDefinition(
            task_key="movie_desc_sync",
            resource_type="movie",
            display_name="影片描述回填",
            default_sort="last_attempted_at:desc",
            allow_reset=True,
            resource_resolver=MOVIE_TASK_RECORD_RESOLVER,
        ),
        "movie_interaction_sync": ResourceTaskDefinition(
            task_key="movie_interaction_sync",
            resource_type="movie",
            display_name="影片互动数同步",
            default_sort="last_attempted_at:desc",
            allow_reset=True,
            resource_resolver=MOVIE_TASK_RECORD_RESOLVER,
        ),
        "movie_desc_translation": ResourceTaskDefinition(
            task_key="movie_desc_translation",
            resource_type="movie",
            display_name="影片简介翻译",
            default_sort="last_attempted_at:desc",
            allow_reset=True,
            resource_resolver=MOVIE_TASK_RECORD_RESOLVER,
        ),
        "movie_title_translation": ResourceTaskDefinition(
            task_key="movie_title_translation",
            resource_type="movie",
            display_name="影片标题翻译",
            default_sort="last_attempted_at:desc",
            allow_reset=True,
            resource_resolver=MOVIE_TASK_RECORD_RESOLVER,
        ),
        "media_thumbnail_generation": ResourceTaskDefinition(
            task_key="media_thumbnail_generation",
            resource_type="media",
            display_name="媒体缩略图生成",
            default_sort="last_attempted_at:desc",
            allow_reset=True,
            resource_resolver=MEDIA_TASK_RECORD_RESOLVER,
        ),
    }

    @classmethod
    def get_definition(cls, task_key: str) -> ResourceTaskDefinition:
        normalized_task_key = str(task_key or "").strip()
        task_definition = cls.TASK_REGISTRY.get(normalized_task_key)
        if task_definition is None:
            raise ValueError(f"resource_task_not_registered: {normalized_task_key}")
        return task_definition

    @classmethod
    def list_definitions(cls) -> list[ResourceTaskDefinition]:
        return list(cls.TASK_REGISTRY.values())

    @classmethod
    def _require_task_definition(cls, task_key: str) -> ResourceTaskDefinition:
        return cls.get_definition(task_key)

    @classmethod
    def _build_default_snapshot(cls, task_definition: ResourceTaskDefinition, resource_id: int) -> ResourceTaskStateSnapshot:
        return ResourceTaskStateSnapshot(
            task_key=task_definition.task_key,
            resource_type=task_definition.resource_type,
            resource_id=int(resource_id),
            state=cls.STATE_PENDING,
        )

    @staticmethod
    def _build_snapshot(record: ResourceTaskState) -> ResourceTaskStateSnapshot:
        return ResourceTaskStateSnapshot(
            task_key=record.task_key,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            state=record.state,
            attempt_count=record.attempt_count,
            last_attempted_at=record.last_attempted_at,
            last_succeeded_at=record.last_succeeded_at,
            last_error=record.last_error,
            last_error_at=record.last_error_at,
            last_task_run_id=record.last_task_run_id,
            last_trigger_type=record.last_trigger_type,
            extra=record.extra,
        )

    @staticmethod
    def _merge_extra(existing_extra: object, extra_patch: dict | None) -> dict | list | None:
        if not extra_patch:
            if isinstance(existing_extra, (dict, list)):
                return existing_extra
            return None

        merged_extra: dict = {}
        if isinstance(existing_extra, dict):
            merged_extra.update(existing_extra)
        # extra 只做浅合并，避免把业务结果对象塞进状态表。
        merged_extra.update(extra_patch)
        return merged_extra

    @classmethod
    def build_retryable_extra_condition(cls, extra_field):
        normalized_extra = fn.COALESCE(extra_field, "")
        terminal_true_condition = None
        for marker in cls.TERMINAL_TRUE_MARKERS:
            marker_condition = normalized_extra.contains(marker)
            terminal_true_condition = (
                marker_condition
                if terminal_true_condition is None
                else (terminal_true_condition | marker_condition)
            )
        # 终态失败统一靠 terminal=true 标记识别；没有该标记的记录继续视为可重试。
        return (normalized_extra == "") | (~terminal_true_condition)

    @staticmethod
    def _reset_extra_for_task(task_key: str, existing_extra: object) -> dict | list | None:
        if not isinstance(existing_extra, dict):
            return existing_extra if isinstance(existing_extra, list) else None
        if task_key not in {"media_thumbnail_generation", "movie_desc_sync"} or "terminal" not in existing_extra:
            return existing_extra

        # 手动重置后需要清掉 terminal 标记，保证自动调度能重新纳入候选。
        next_extra = dict(existing_extra)
        next_extra.pop("terminal", None)
        return next_extra or None

    @staticmethod
    def _resolve_task_runtime_context(
        *,
        task_key: str,
        trigger_type: str | None,
        task_run_id: int | None,
    ) -> tuple[str | None, int | None]:
        task_run_context = ActivityService.get_task_run_context()
        if task_run_context is None or task_run_context.task_key != task_key:
            return trigger_type, task_run_id
        resolved_trigger_type = trigger_type if trigger_type is not None else task_run_context.trigger_type
        resolved_task_run_id = task_run_id if task_run_id is not None else task_run_context.task_run_id
        return resolved_trigger_type, resolved_task_run_id

    @classmethod
    def _resolve_sort(cls, task_definition: ResourceTaskDefinition, sort: str | None) -> Sequence:
        return resolve_sort(
            sort,
            cls.TASK_STATE_SORT_FIELDS,
            default_key=task_definition.default_sort,
            error_code="invalid_resource_task_state_filter",
        )

    @staticmethod
    def _validate_page(page: int, page_size: int) -> None:
        validate_page(page, page_size, error_code="invalid_resource_task_state_filter")

    @classmethod
    def _get_or_create_record(cls, task_key: str, resource_id: int) -> ResourceTaskState:
        task_definition = cls._require_task_definition(task_key)
        normalized_resource_id = int(resource_id)
        query = ResourceTaskState.select().where(
            ResourceTaskState.task_key == task_definition.task_key,
            ResourceTaskState.resource_type == task_definition.resource_type,
            ResourceTaskState.resource_id == normalized_resource_id,
        )
        record = query.get_or_none()
        if record is not None:
            return record

        database = get_database()
        with database.atomic():
            try:
                return ResourceTaskState.create(
                    task_key=task_definition.task_key,
                    resource_type=task_definition.resource_type,
                    resource_id=normalized_resource_id,
                )
            except IntegrityError:
                return query.get()

    @classmethod
    def get_state(cls, task_key: str, resource_id: int) -> ResourceTaskState | None:
        task_definition = cls._require_task_definition(task_key)
        return ResourceTaskState.get_or_none(
            ResourceTaskState.task_key == task_definition.task_key,
            ResourceTaskState.resource_type == task_definition.resource_type,
            ResourceTaskState.resource_id == int(resource_id),
        )

    @classmethod
    def get_state_or_default(cls, task_key: str, resource_id: int) -> ResourceTaskStateSnapshot:
        task_definition = cls._require_task_definition(task_key)
        record = cls.get_state(task_key, resource_id)
        if record is None:
            return cls._build_default_snapshot(task_definition, int(resource_id))
        return cls._build_snapshot(record)

    @classmethod
    def mark_started(
        cls,
        task_key: str,
        resource_id: int,
        trigger_type: str | None = None,
        task_run_id: int | None = None,
    ) -> ResourceTaskState:
        trigger_type, task_run_id = cls._resolve_task_runtime_context(
            task_key=task_key,
            trigger_type=trigger_type,
            task_run_id=task_run_id,
        )
        record = cls._get_or_create_record(task_key, resource_id)
        now = utc_now_for_db()
        record.state = cls.STATE_RUNNING
        record.attempt_count += 1
        record.last_attempted_at = now
        record.last_error = None
        record.last_trigger_type = trigger_type
        record.last_task_run_id = task_run_id
        record.updated_at = now
        record.save(
            only=[
                ResourceTaskState.state,
                ResourceTaskState.attempt_count,
                ResourceTaskState.last_attempted_at,
                ResourceTaskState.last_error,
                ResourceTaskState.last_trigger_type,
                ResourceTaskState.last_task_run_id,
                ResourceTaskState.updated_at,
            ]
        )
        return record

    @classmethod
    def mark_succeeded(
        cls,
        task_key: str,
        resource_id: int,
        trigger_type: str | None = None,
        task_run_id: int | None = None,
        extra_patch: dict | None = None,
    ) -> ResourceTaskState:
        trigger_type, task_run_id = cls._resolve_task_runtime_context(
            task_key=task_key,
            trigger_type=trigger_type,
            task_run_id=task_run_id,
        )
        record = cls._get_or_create_record(task_key, resource_id)
        now = utc_now_for_db()
        record.state = cls.STATE_SUCCEEDED
        record.last_succeeded_at = now
        record.last_error = None
        record.last_trigger_type = trigger_type
        record.last_task_run_id = task_run_id
        record.extra = cls._merge_extra(record.extra, extra_patch)
        record.updated_at = now
        record.save(
            only=[
                ResourceTaskState.state,
                ResourceTaskState.last_succeeded_at,
                ResourceTaskState.last_error,
                ResourceTaskState.last_trigger_type,
                ResourceTaskState.last_task_run_id,
                ResourceTaskState.extra,
                ResourceTaskState.updated_at,
            ]
        )
        return record

    @classmethod
    def mark_failed(
        cls,
        task_key: str,
        resource_id: int,
        detail: str,
        trigger_type: str | None = None,
        task_run_id: int | None = None,
        extra_patch: dict | None = None,
    ) -> ResourceTaskState:
        trigger_type, task_run_id = cls._resolve_task_runtime_context(
            task_key=task_key,
            trigger_type=trigger_type,
            task_run_id=task_run_id,
        )
        record = cls._get_or_create_record(task_key, resource_id)
        now = utc_now_for_db()
        record.state = cls.STATE_FAILED
        record.last_error = detail
        record.last_error_at = now
        record.last_trigger_type = trigger_type
        record.last_task_run_id = task_run_id
        record.extra = cls._merge_extra(record.extra, extra_patch)
        record.updated_at = now
        record.save(
            only=[
                ResourceTaskState.state,
                ResourceTaskState.last_error,
                ResourceTaskState.last_error_at,
                ResourceTaskState.last_trigger_type,
                ResourceTaskState.last_task_run_id,
                ResourceTaskState.extra,
                ResourceTaskState.updated_at,
            ]
        )
        return record

    @classmethod
    def mark_pending(
        cls,
        task_key: str,
        resource_id: int,
        detail: str | None = None,
        trigger_type: str | None = None,
        task_run_id: int | None = None,
    ) -> ResourceTaskState:
        trigger_type, task_run_id = cls._resolve_task_runtime_context(
            task_key=task_key,
            trigger_type=trigger_type,
            task_run_id=task_run_id,
        )
        record = cls._get_or_create_record(task_key, resource_id)
        now = utc_now_for_db()
        record.state = cls.STATE_PENDING
        record.last_trigger_type = trigger_type
        record.last_task_run_id = task_run_id
        record.updated_at = now
        fields = [
            ResourceTaskState.state,
            ResourceTaskState.last_trigger_type,
            ResourceTaskState.last_task_run_id,
            ResourceTaskState.updated_at,
        ]
        if detail is not None:
            record.last_error = detail
            record.last_error_at = now
            fields.extend(
                [
                    ResourceTaskState.last_error,
                    ResourceTaskState.last_error_at,
                ]
            )
        record.save(only=fields)
        return record

    @classmethod
    def reset_failed(cls, task_key: str, resource_id: int) -> ResourceTaskState:
        task_definition = cls._require_task_definition(task_key)
        if not task_definition.allow_reset:
            raise ApiError(
                422,
                "resource_task_state_reset_forbidden",
                "当前任务不支持重置失败记录",
                {"task_key": task_key, "resource_id": int(resource_id)},
            )
        record = cls.get_state(task_key, resource_id)
        if record is None:
            raise ApiError(
                404,
                "resource_task_state_not_found",
                "资源任务记录不存在",
                {"task_key": task_key, "resource_id": int(resource_id)},
            )
        if record.state != cls.STATE_FAILED:
            raise ApiError(
                422,
                "resource_task_state_reset_forbidden",
                "仅允许重置失败记录",
                {"task_key": task_key, "resource_id": int(resource_id), "state": record.state},
            )
        now = utc_now_for_db()
        record.state = cls.STATE_PENDING
        record.attempt_count = 0
        record.last_error = None
        record.last_error_at = None
        record.last_trigger_type = "manual"
        record.last_task_run_id = None
        record.updated_at = now
        fields = [
            ResourceTaskState.state,
            ResourceTaskState.attempt_count,
            ResourceTaskState.last_error,
            ResourceTaskState.last_error_at,
            ResourceTaskState.last_trigger_type,
            ResourceTaskState.last_task_run_id,
            ResourceTaskState.updated_at,
        ]
        reset_extra = cls._reset_extra_for_task(task_key, record.extra)
        if reset_extra != record.extra:
            record.extra = reset_extra
            fields.append(ResourceTaskState.extra)
        record.save(only=fields)
        return record

    @classmethod
    def reset_for_requeue(cls, task_key: str, resource_id: int) -> ResourceTaskState:
        record = cls._get_or_create_record(task_key, resource_id)
        now = utc_now_for_db()
        # 资源进入新一轮处理时，需要清空上一轮尝试痕迹，避免新文件继承旧结果。
        record.state = cls.STATE_PENDING
        record.attempt_count = 0
        record.last_attempted_at = None
        record.last_succeeded_at = None
        record.last_error = None
        record.last_error_at = None
        record.last_trigger_type = None
        record.last_task_run_id = None
        record.extra = None
        record.updated_at = now
        record.save(
            only=[
                ResourceTaskState.state,
                ResourceTaskState.attempt_count,
                ResourceTaskState.last_attempted_at,
                ResourceTaskState.last_succeeded_at,
                ResourceTaskState.last_error,
                ResourceTaskState.last_error_at,
                ResourceTaskState.last_trigger_type,
                ResourceTaskState.last_task_run_id,
                ResourceTaskState.extra,
                ResourceTaskState.updated_at,
            ]
        )
        return record

    @classmethod
    def list_definition_resources(cls) -> list[ResourceTaskDefinitionResource]:
        counts_by_task_key = {
            definition.task_key: TaskRecordStateCountsResource()
            for definition in cls.list_definitions()
        }
        query = (
            ResourceTaskState.select(
                ResourceTaskState.task_key,
                ResourceTaskState.state,
                fn.COUNT(ResourceTaskState.id).alias("total"),
            )
            .where(ResourceTaskState.task_key.in_(tuple(cls.TASK_REGISTRY.keys())))
            .group_by(ResourceTaskState.task_key, ResourceTaskState.state)
        )
        for row in query:
            state_counts = counts_by_task_key.get(row.task_key)
            if state_counts is None or row.state not in TaskRecordStateCountsResource.model_fields:
                continue
            setattr(state_counts, row.state, int(row.total))
        return [
            ResourceTaskDefinitionResource(
                task_key=definition.task_key,
                resource_type=definition.resource_type,
                display_name=definition.display_name,
                default_sort=definition.default_sort,
                allow_reset=definition.allow_reset,
                state_counts=counts_by_task_key[definition.task_key],
            )
            for definition in cls.list_definitions()
        ]

    @classmethod
    def list_record_resources(
        cls,
        *,
        task_key: str,
        page: int = 1,
        page_size: int = 20,
        state: str | None = None,
        search: str | None = None,
        sort: str | None = None,
    ) -> PageResponse[ResourceTaskRecordResource]:
        cls._validate_page(page, page_size)
        task_definition = cls._require_task_definition(task_key)
        query = ResourceTaskState.select().where(
            ResourceTaskState.task_key == task_definition.task_key,
            ResourceTaskState.resource_type == task_definition.resource_type,
        )

        normalized_state = str(state or "").strip().lower()
        if normalized_state:
            if normalized_state not in {
                cls.STATE_PENDING,
                cls.STATE_RUNNING,
                cls.STATE_SUCCEEDED,
                cls.STATE_FAILED,
            }:
                raise ApiError(
                    422,
                    "invalid_resource_task_state_filter",
                    "state is invalid",
                    {"state": state},
                )
            query = query.where(ResourceTaskState.state == normalized_state)

        normalized_search = str(search or "").strip()
        if normalized_search:
            resolver = task_definition.resource_resolver
            if resolver is None:
                raise ApiError(
                    422,
                    "resource_task_state_search_unsupported",
                    "当前任务不支持搜索",
                    {"task_key": task_key},
                )
            matched_resource_ids = resolver.search_resource_ids(normalized_search)
            if not matched_resource_ids:
                return PageResponse[ResourceTaskRecordResource](
                    items=[],
                    page=page,
                    page_size=page_size,
                    total=0,
                )
            query = query.where(ResourceTaskState.resource_id.in_(matched_resource_ids))

        total = query.count()
        start = (page - 1) * page_size
        order_by = cls._resolve_sort(task_definition, sort)
        records = list(query.order_by(*order_by).offset(start).limit(page_size))
        resource_summaries = {}
        if records and task_definition.resource_resolver is not None:
            resource_summaries = task_definition.resource_resolver.resolve_summaries(
                [record.resource_id for record in records]
            )
        return PageResponse[ResourceTaskRecordResource](
            items=[
                ResourceTaskRecordResource(
                    task_key=record.task_key,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    state=record.state,
                    attempt_count=record.attempt_count,
                    last_attempted_at=record.last_attempted_at,
                    last_succeeded_at=record.last_succeeded_at,
                    last_error=record.last_error,
                    last_error_at=record.last_error_at,
                    last_task_run_id=record.last_task_run_id,
                    last_trigger_type=record.last_trigger_type,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    resource=resource_summaries.get(record.resource_id),
                )
                for record in records
            ],
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_record_resource(cls, task_key: str, resource_id: int) -> ResourceTaskRecordResource:
        task_definition = cls._require_task_definition(task_key)
        record = cls.get_state(task_key, resource_id)
        if record is None:
            raise ApiError(
                404,
                "resource_task_state_not_found",
                "资源任务记录不存在",
                {"task_key": task_key, "resource_id": int(resource_id)},
            )
        resource_summary = None
        if task_definition.resource_resolver is not None:
            resource_summary = task_definition.resource_resolver.resolve_summaries([int(resource_id)]).get(int(resource_id))
        return ResourceTaskRecordResource(
            task_key=record.task_key,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            state=record.state,
            attempt_count=record.attempt_count,
            last_attempted_at=record.last_attempted_at,
            last_succeeded_at=record.last_succeeded_at,
            last_error=record.last_error,
            last_error_at=record.last_error_at,
            last_task_run_id=record.last_task_run_id,
            last_trigger_type=record.last_trigger_type,
            created_at=record.created_at,
            updated_at=record.updated_at,
            resource=resource_summary,
        )

    @classmethod
    def recover_running_records(
        cls,
        task_key: str,
        error_message: str,
        trigger_type: str = "startup",
    ) -> int:
        task_definition = cls._require_task_definition(task_key)
        now = utc_now_for_db()
        # 启动恢复只回收 running 记录，避免误改待处理和已完成状态。
        return (
            ResourceTaskState.update(
                state=cls.STATE_FAILED,
                last_error=error_message,
                last_error_at=now,
                last_trigger_type=trigger_type,
                updated_at=now,
            )
            .where(
                ResourceTaskState.task_key == task_definition.task_key,
                ResourceTaskState.resource_type == task_definition.resource_type,
                ResourceTaskState.state == cls.STATE_RUNNING,
            )
            .execute()
        )
