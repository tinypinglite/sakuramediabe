from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from peewee import IntegrityError, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import resolve_sort, validate_page
from src.model import ResourceTaskState
from src.model.base import get_database
from src.schema.common.pagination import PageResponse
from src.schema.system.resource_task_state import (
    ResourceTaskDefinitionResource,
    ResourceTaskRecordResource,
    TaskRecordStateCountsResource,
)
from src.service.system.resource_task_actions_registry import (
    ACTION_RESET_RETRY_BUDGET,
    ACTION_RETRY_NOW,
    SUPPORTED_ACTIONS,
    available_actions_for_state,
)
from src.service.system.resource_task_resolvers import (
    MEDIA_TASK_RECORD_RESOLVER,
    MOVIE_TASK_RECORD_RESOLVER,
    ResourceTaskRecordResolver,
    build_movie_actionable_check,
    media_actionable_check,
)


@dataclass(frozen=True)
class ResourceTaskDefinition:
    task_key: str
    resource_type: str
    display_name: str
    default_sort: str
    resource_resolver: ResourceTaskRecordResolver | None = None
    # 该任务开放的统一 action 集合：rerun（强制重跑）只有域语义安全的任务声明。
    supported_actions: tuple[str, ...] = SUPPORTED_ACTIONS
    # 领域合格性钩子：action 入口先跑它过滤领域上不成立的资源（影片缺字段 / 媒体失效等）。
    check_actionable: Callable[[list[int]], dict[int, str]] | None = None
    deferred_limit: int = 0


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
    # 已达到自动重试上限：调度器不再自动排入，只有用户显式重置才会回到 pending。
    STATE_EXHAUSTED = "exhausted"
    TASK_STATE_SORT_FIELDS = {
        "last_attempted_at:desc": (ResourceTaskState.last_attempted_at.desc(), ResourceTaskState.id.desc()),
        "last_attempted_at:asc": (ResourceTaskState.last_attempted_at.asc(), ResourceTaskState.id.asc()),
        "last_error_at:desc": (ResourceTaskState.last_error_at.desc(), ResourceTaskState.id.desc()),
        "attempt_count:desc": (ResourceTaskState.attempt_count.desc(), ResourceTaskState.id.desc()),
        "updated_at:desc": (ResourceTaskState.updated_at.desc(), ResourceTaskState.id.desc()),
        "updated_at:asc": (ResourceTaskState.updated_at.asc(), ResourceTaskState.id.asc()),
    }
    TASK_REGISTRY = {
        "movie_interaction_sync": ResourceTaskDefinition(
            task_key="movie_interaction_sync",
            resource_type="movie",
            display_name="影片互动数同步",
            default_sort="last_attempted_at:desc",
            resource_resolver=MOVIE_TASK_RECORD_RESOLVER,
            check_actionable=build_movie_actionable_check(
                required_attr="javdb_id",
                missing_reason="movie_javdb_id_missing",
            ),
        ),
        "media_thumbnail_generation": ResourceTaskDefinition(
            task_key="media_thumbnail_generation",
            resource_type="media",
            display_name="媒体缩略图生成",
            default_sort="last_attempted_at:desc",
            resource_resolver=MEDIA_TASK_RECORD_RESOLVER,
            # 不开放 rerun：缩略图不支持覆盖再生（已存在即跳过），rerun 只会空转。
            supported_actions=(ACTION_RETRY_NOW, ACTION_RESET_RETRY_BUDGET),
            check_actionable=media_actionable_check,
            deferred_limit=5,
        ),
        # Wave 2：task_key 与 job 合并（原 subscribed_movie_search，历史行随迁移清空）。
        "subscribed_movie_auto_download": ResourceTaskDefinition(
            task_key="subscribed_movie_auto_download",
            resource_type="movie",
            display_name="订阅影片资源查询",
            default_sort="last_attempted_at:desc",
            resource_resolver=MOVIE_TASK_RECORD_RESOLVER,
            # 不开放 rerun：强制重搜会绕过"已有媒体 / 已有活跃下载"防护，重复提交下载。
            supported_actions=(ACTION_RETRY_NOW, ACTION_RESET_RETRY_BUDGET),
            check_actionable=build_movie_actionable_check(require_subscribed=True),
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
    def get_or_create_record(cls, task_key: str, resource_id: int) -> ResourceTaskState:
        """按 (task_key, resource_id) 取状态行，不存在则建。

        对外公开，供需要自定义记账语义（不走 mark_started/succeeded/failed 那套通用流转）的
        任务复用，例如订阅影片资源查询的退避状态机。并发建行由唯一索引拦截后回读。
        """
        task_definition = cls.get_definition(task_key)
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
        task_definition = cls.get_definition(task_key)
        return ResourceTaskState.get_or_none(
            ResourceTaskState.task_key == task_definition.task_key,
            ResourceTaskState.resource_type == task_definition.resource_type,
            ResourceTaskState.resource_id == int(resource_id),
        )

    @classmethod
    def get_state_or_default(cls, task_key: str, resource_id: int) -> ResourceTaskStateSnapshot:
        task_definition = cls.get_definition(task_key)
        record = cls.get_state(task_key, resource_id)
        if record is None:
            return cls._build_default_snapshot(task_definition, int(resource_id))
        return cls._build_snapshot(record)

    @classmethod
    def reset_for_requeue(cls, task_key: str, resource_id: int) -> ResourceTaskState:
        record = cls.get_or_create_record(task_key, resource_id)
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
                supported_actions=list(definition.supported_actions),
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
        validate_page(page, page_size, error_code="invalid_resource_task_state_filter")
        task_definition = cls.get_definition(task_key)
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
                # kernel 记账任务（Wave 2 起）的失败二分状态。
                "failed_retryable",
                "failed_terminal",
                cls.STATE_EXHAUSTED,
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
        order_by = resolve_sort(
            sort,
            cls.TASK_STATE_SORT_FIELDS,
            default_key=task_definition.default_sort,
            error_code="invalid_resource_task_state_filter",
        )
        records = list(query.order_by(*order_by).offset(start).limit(page_size))
        resource_summaries = {}
        if records and task_definition.resource_resolver is not None:
            resource_summaries = task_definition.resource_resolver.resolve_summaries(
                [record.resource_id for record in records]
            )
        def deferred_metadata(record: ResourceTaskState) -> tuple[int, str | None]:
            if not isinstance(record.extra, dict):
                return 0, None
            count = record.extra.get("deferred_count")
            reason = record.extra.get("deferred_reason")
            return (
                count if isinstance(count, int) and count > 0 else 0,
                reason if isinstance(reason, str) and reason.strip() else None,
            )
        return PageResponse[ResourceTaskRecordResource](
            items=[
                ResourceTaskRecordResource(
                    task_key=record.task_key,
                    resource_type=record.resource_type,
                    resource_id=record.resource_id,
                    state=record.state,
                    attempt_count=record.attempt_count,
                    deferred_count=deferred_metadata(record)[0],
                    deferred_limit=task_definition.deferred_limit,
                    deferred_reason=deferred_metadata(record)[1],
                    next_retry_at=record.next_retry_at,
                    last_attempted_at=record.last_attempted_at,
                    last_succeeded_at=record.last_succeeded_at,
                    last_error=record.last_error,
                    last_error_at=record.last_error_at,
                    last_task_run_id=record.last_task_run_id,
                    last_trigger_type=record.last_trigger_type,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    resource=resource_summaries.get(record.resource_id),
                    available_actions=available_actions_for_state(
                        record.state, task_definition.supported_actions
                    ),
                )
                for record in records
            ],
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_record_resource(cls, task_key: str, resource_id: int) -> ResourceTaskRecordResource:
        task_definition = cls.get_definition(task_key)
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
        extra = record.extra if isinstance(record.extra, dict) else {}
        deferred_count = extra.get("deferred_count")
        deferred_reason = extra.get("deferred_reason")
        return ResourceTaskRecordResource(
            task_key=record.task_key,
            resource_type=record.resource_type,
            resource_id=record.resource_id,
            state=record.state,
            attempt_count=record.attempt_count,
            deferred_count=deferred_count if isinstance(deferred_count, int) and deferred_count > 0 else 0,
            deferred_limit=task_definition.deferred_limit,
            deferred_reason=deferred_reason if isinstance(deferred_reason, str) and deferred_reason.strip() else None,
            next_retry_at=record.next_retry_at,
            last_attempted_at=record.last_attempted_at,
            last_succeeded_at=record.last_succeeded_at,
            last_error=record.last_error,
            last_error_at=record.last_error_at,
            last_task_run_id=record.last_task_run_id,
            last_trigger_type=record.last_trigger_type,
            created_at=record.created_at,
            updated_at=record.updated_at,
            resource=resource_summary,
            available_actions=available_actions_for_state(
                record.state, task_definition.supported_actions
            ),
        )
