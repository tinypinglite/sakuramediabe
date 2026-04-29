from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from peewee import IntegrityError, MySQLDatabase, PostgresqlDatabase, SqliteDatabase, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun, SystemEvent, SystemNotification
from src.model.base import get_database
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import (
    ActivityBootstrapResource,
    NotificationArchiveResponse,
    NotificationReadResponse,
    NotificationResource,
    NotificationUnreadCountResource,
    SystemEventEnvelope,
    TaskRunResource,
)

ALLOWED_NOTIFICATION_CATEGORIES = {"reminder", "info", "warning", "error"}
ALLOWED_TASK_TRIGGER_TYPES = {"scheduled", "manual", "startup", "internal"}
ALLOWED_TASK_STATES = {"pending", "running", "completed", "failed"}
TASK_RUN_SORT_FIELDS = {
    "started_at:desc": (BackgroundTaskRun.started_at.desc(), BackgroundTaskRun.id.desc()),
    "started_at:asc": (BackgroundTaskRun.started_at.asc(), BackgroundTaskRun.id.asc()),
    "created_at:desc": (BackgroundTaskRun.created_at.desc(), BackgroundTaskRun.id.desc()),
    "created_at:asc": (BackgroundTaskRun.created_at.asc(), BackgroundTaskRun.id.asc()),
    "updated_at:desc": (BackgroundTaskRun.updated_at.desc(), BackgroundTaskRun.id.desc()),
    "updated_at:asc": (BackgroundTaskRun.updated_at.asc(), BackgroundTaskRun.id.asc()),
}
ACTIVITY_BOOTSTRAP_PAGE_SIZE = 20

TASK_NAME_REGISTRY = {
    "actor_subscription_sync": "订阅演员影片同步",
    "subscribed_movie_auto_download": "已订阅缺失影片自动下载",
    "movie_heat_update": "影片热度更新",
    "movie_interaction_sync": "影片互动数同步",
    "ranking_sync": "排行榜同步",
    "hot_review_sync": "JavDB 热评同步",
    "movie_collection_sync": "合集影片同步",
    "movie_desc_sync": "影片描述回填",
    "movie_desc_translation": "影片简介翻译",
    "movie_title_translation": "影片标题翻译",
    "movie_similarity_recompute": "影片相似度重算",
    "download_task_sync": "下载任务状态同步",
    "download_task_auto_import": "已完成下载自动导入",
    "media_thumbnail_generation": "媒体缩略图生成",
    "image_search_index": "图像搜索索引构建",
    "image_search_optimize": "图像搜索索引优化",
    "download_task_import": "下载任务导入",
}

ALLOWED_TASK_CONFLICT_POLICIES = {"raise", "skip"}


@dataclass(frozen=True)
class TaskRunContext:
    task_key: str
    task_run_id: int
    trigger_type: str


TASK_RUN_CONTEXT: ContextVar[TaskRunContext | None] = ContextVar("task_run_context", default=None)


def _now() -> datetime:
    return utc_now_for_db()


def _validate_page(page: int, page_size: int) -> None:
    if page <= 0 or page_size <= 0 or page_size > 100:
        raise ApiError(
            422,
            "invalid_pagination",
            "page and page_size must be valid positive integers",
            {"page": page, "page_size": page_size},
        )


def _normalize_string_filter(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def _normalize_allowed_filter(
    value: str | None,
    *,
    field_name: str,
    allowed_values: set[str],
) -> str | None:
    normalized = _normalize_string_filter(value)
    if normalized is None:
        return None
    normalized = normalized.lower()
    if normalized not in allowed_values:
        raise ApiError(
            422,
            "invalid_activity_filter",
            f"{field_name} is invalid",
            {"field_name": field_name, "value": value, "allowed_values": sorted(allowed_values)},
        )
    return normalized


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _format_result_text(summary: dict[str, Any] | None) -> str | None:
    if not summary:
        return None
    fragments: list[str] = []
    for key, value in summary.items():
        if isinstance(value, (dict, list)):
            continue
        if value is None:
            continue
        fragments.append(f"{key}={_format_scalar(value)}")
    return " ".join(fragments) if fragments else None


def _detect_warning_summary(summary: dict[str, Any] | None) -> bool:
    if not summary:
        return False
    for key, value in summary.items():
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        if "failed" in key or "skipped" in key:
            return True
    return False


def _merge_summary(base_summary: dict[str, Any], summary_patch: dict[str, Any] | None) -> dict[str, Any]:
    if not summary_patch:
        return dict(base_summary)
    merged = dict(base_summary)
    for key, value in summary_patch.items():
        # 核心统计字段允许任务逐步覆盖或补齐，保证前端始终看到最新汇总。
        merged[key] = value
    return merged


class TaskRunConflictError(RuntimeError):
    def __init__(self, blocking_task_run: BackgroundTaskRun):
        self.blocking_task_run = blocking_task_run
        super().__init__(self.format_message(blocking_task_run))

    @staticmethod
    def format_message(task_run: BackgroundTaskRun) -> str:
        started_at_text = task_run.started_at.isoformat(sep=" ", timespec="seconds") if task_run.started_at else "未知"
        return (
            f"任务“{task_run.task_name}”已在运行中，"
            f"trigger_type={task_run.trigger_type} task_run_id={task_run.id} started_at={started_at_text}"
        )


def _build_task_skip_result(blocking_task_run: BackgroundTaskRun) -> dict[str, Any]:
    return {
        "task_skipped": True,
        "reason": "mutex_conflict",
        "blocking_task_run_id": blocking_task_run.id,
        "blocking_task_key": blocking_task_run.task_key,
        "blocking_trigger_type": blocking_task_run.trigger_type,
        "blocking_started_at": blocking_task_run.started_at.isoformat() if blocking_task_run.started_at else None,
        "blocking_task_name": blocking_task_run.task_name,
    }


@contextmanager
def _activity_read_snapshot():
    database = get_database()
    if isinstance(database, SqliteDatabase):
        with database.atomic():
            yield
        return

    if isinstance(database, (MySQLDatabase, PostgresqlDatabase)):
        # 活动中心首屏需要同一读快照，避免多次 SELECT 之间看到不同时间点的数据。
        with database.atomic(isolation_level="repeatable read"):
            yield
        return

    with database.atomic():
        yield


class TaskRunReporter(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_run_id: int
    summary: dict[str, Any] = Field(default_factory=dict)
    extra_callbacks: list[Callable[[dict[str, Any]], None]] = Field(default_factory=list)

    def emit(
        self,
        *,
        current: int | None = None,
        total: int | None = None,
        text: str | None = None,
        summary_patch: dict[str, Any] | None = None,
    ) -> None:
        if summary_patch:
            self.summary = _merge_summary(self.summary, summary_patch)
        ActivityService.update_task_run_progress(
            self.task_run_id,
            current=current,
            total=total,
            text=text,
            summary_patch=summary_patch,
        )

    def progress_callback(self, payload: dict[str, Any]) -> None:
        self.emit(
            current=payload.get("current"),
            total=payload.get("total"),
            text=payload.get("text"),
            summary_patch=payload.get("summary_patch"),
        )
        for cb in self.extra_callbacks:
            cb(payload)


class SystemEventService:
    @staticmethod
    def publish(
        *,
        event_type: str,
        payload: dict[str, Any],
        resource_type: str | None = None,
        resource_id: int | None = None,
    ) -> SystemEvent:
        return SystemEvent.create(
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            payload=payload,
        )

    @staticmethod
    def list_after(event_id: int, limit: int = 100) -> list[SystemEventEnvelope]:
        query = (
            SystemEvent.select()
            .where(SystemEvent.id > max(int(event_id), 0))
            .order_by(SystemEvent.id.asc())
            .limit(max(1, limit))
        )
        return [
            SystemEventEnvelope(
                event_id=event.id,
                event=event.event_type,
                data=event.payload or {},
            )
            for event in query
        ]

    @classmethod
    def stream(
        cls,
        *,
        after_event_id: int = 0,
        poll_interval_seconds: float = 1.0,
        heartbeat_interval_seconds: float = 15.0,
    ):
        last_event_id = max(int(after_event_id), 0)
        last_heartbeat_at = time.time()
        while True:
            # SSE 只负责在线增量，因此这里按事件表顺序追增量，不做历史重放拼装。
            events = cls.list_after(last_event_id)
            if events:
                for event in events:
                    last_event_id = event.event_id
                    yield (
                        f"id: {event.event_id}\n"
                        f"event: {event.event}\n"
                        f"data: {json.dumps(event.data, ensure_ascii=False)}\n\n"
                    )
                last_heartbeat_at = time.time()
                continue

            now = time.time()
            if now - last_heartbeat_at >= heartbeat_interval_seconds:
                yield "event: heartbeat\ndata: {}\n\n"
                last_heartbeat_at = now
            time.sleep(max(poll_interval_seconds, 0.1))


class ActivityService:
    @staticmethod
    def get_task_run_context() -> TaskRunContext | None:
        return TASK_RUN_CONTEXT.get()

    @staticmethod
    def set_task_run_context(*, task_key: str, task_run_id: int, trigger_type: str) -> Token:
        return TASK_RUN_CONTEXT.set(
            TaskRunContext(
                task_key=task_key,
                task_run_id=task_run_id,
                trigger_type=trigger_type,
            )
        )

    @staticmethod
    def reset_task_run_context(token: Token) -> None:
        TASK_RUN_CONTEXT.reset(token)

    @staticmethod
    def wrap_current_task_run_context(func: Callable[..., Any]) -> Callable[..., Any]:
        # 线程池不会自动继承 ContextVar，这里在提交任务时显式复制上下文。
        runtime_context = copy_context()

        def _run_with_context(*args, **kwargs):
            return runtime_context.run(func, *args, **kwargs)

        return _run_with_context

    @staticmethod
    def _is_process_alive(pid: int | None) -> bool:
        if pid is None or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @staticmethod
    def _task_run_resource(task_run: BackgroundTaskRun) -> TaskRunResource:
        return TaskRunResource.model_validate(task_run)

    @staticmethod
    def _notification_resource(notification: SystemNotification) -> NotificationResource:
        return NotificationResource.model_validate(
            {
                "id": notification.id,
                "category": notification.category,
                "title": notification.title,
                "content": notification.content,
                "is_read": notification.is_read,
                "archived": notification.archived_at is not None,
                "created_at": notification.created_at,
                "updated_at": notification.updated_at,
                "related_task_run_id": notification.related_task_run_id,
                "related_resource_type": notification.related_resource_type,
                "related_resource_id": notification.related_resource_id,
            }
        )

    @staticmethod
    def _resolve_task_name(task_key: str, task_name: str | None = None) -> str:
        return (task_name or "").strip() or TASK_NAME_REGISTRY.get(task_key, task_key)

    @classmethod
    def _build_notification_query(
        cls,
        *,
        category: str | None = None,
        is_read: bool | None = None,
        archived: bool = False,
    ):
        normalized_category = _normalize_allowed_filter(
            category,
            field_name="category",
            allowed_values=ALLOWED_NOTIFICATION_CATEGORIES,
        )
        query = SystemNotification.select().order_by(SystemNotification.created_at.desc(), SystemNotification.id.desc())
        if normalized_category is not None:
            query = query.where(SystemNotification.category == normalized_category)
        if is_read is not None:
            query = query.where(SystemNotification.is_read == is_read)
        if archived:
            return query.where(SystemNotification.archived_at.is_null(False))
        return query.where(SystemNotification.archived_at.is_null(True))

    @classmethod
    def _page_notifications(cls, query, *, page: int, page_size: int) -> PageResponse[NotificationResource]:
        total = query.count()
        start = (page - 1) * page_size
        items = [cls._notification_resource(item) for item in query.offset(start).limit(page_size)]
        return PageResponse[NotificationResource](items=items, page=page, page_size=page_size, total=total)

    @classmethod
    def _build_task_run_query(
        cls,
        *,
        state: str | None = None,
        task_key: str | None = None,
        trigger_type: str | None = None,
        sort: str | None = None,
    ):
        normalized_state = _normalize_allowed_filter(
            state,
            field_name="state",
            allowed_values=ALLOWED_TASK_STATES,
        )
        normalized_trigger_type = _normalize_allowed_filter(
            trigger_type,
            field_name="trigger_type",
            allowed_values=ALLOWED_TASK_TRIGGER_TYPES,
        )
        normalized_task_key = _normalize_string_filter(task_key)
        order_by = TASK_RUN_SORT_FIELDS.get((sort or "started_at:desc").strip().lower())
        if order_by is None:
            raise ApiError(
                422,
                "invalid_task_run_sort",
                "任务排序规则不合法",
                {"sort": sort, "allowed_values": sorted(TASK_RUN_SORT_FIELDS)},
            )

        query = BackgroundTaskRun.select()
        if normalized_state is not None:
            query = query.where(BackgroundTaskRun.state == normalized_state)
        if normalized_trigger_type is not None:
            query = query.where(BackgroundTaskRun.trigger_type == normalized_trigger_type)
        if normalized_task_key is not None:
            query = query.where(BackgroundTaskRun.task_key == normalized_task_key)
        return query.order_by(*order_by)

    @classmethod
    def _page_task_runs(cls, query, *, page: int, page_size: int) -> PageResponse[TaskRunResource]:
        total = query.count()
        start = (page - 1) * page_size
        items = [cls._task_run_resource(item) for item in query.offset(start).limit(page_size)]
        return PageResponse[TaskRunResource](items=items, page=page, page_size=page_size, total=total)

    @staticmethod
    def create_task_run(
        *,
        task_key: str,
        task_name: str | None = None,
        trigger_type: str,
        state: str = "pending",
        owner_pid: int | None = None,
        mutex_key: str | None = None,
    ) -> BackgroundTaskRun:
        normalized_trigger_type = _normalize_allowed_filter(
            trigger_type,
            field_name="trigger_type",
            allowed_values=ALLOWED_TASK_TRIGGER_TYPES,
        )
        normalized_state = _normalize_allowed_filter(
            state,
            field_name="state",
            allowed_values=ALLOWED_TASK_STATES,
        )
        with get_database().atomic():
            task_run = BackgroundTaskRun.create(
                task_key=task_key,
                task_name=ActivityService._resolve_task_name(task_key, task_name),
                trigger_type=normalized_trigger_type or "internal",
                owner_pid=os.getpid() if owner_pid is None else owner_pid,
                mutex_key=_normalize_string_filter(mutex_key),
                state=normalized_state or "pending",
                started_at=_now() if normalized_state == "running" else None,
                result_summary={},
            )
            # 任务中心依赖这条 created 事件拿到新增任务，避免前端只能靠轮询发现新任务。
            SystemEventService.publish(
                event_type="task_run_created",
                payload=ActivityService._task_run_resource(task_run).model_dump(mode="json"),
                resource_type="task_run",
                resource_id=task_run.id,
            )
            return task_run

    @staticmethod
    def mark_task_run_running(task_run_id: int) -> BackgroundTaskRun:
        with get_database().atomic():
            task_run = BackgroundTaskRun.get_by_id(task_run_id)
            task_run.state = "running"
            if task_run.started_at is None:
                task_run.started_at = _now()
            task_run.updated_at = _now()
            task_run.save()
            SystemEventService.publish(
                event_type="task_run_updated",
                payload=ActivityService._task_run_resource(task_run).model_dump(mode="json"),
                resource_type="task_run",
                resource_id=task_run.id,
            )
            return task_run

    @staticmethod
    def update_task_run_progress(
        task_run_id: int,
        *,
        current: int | None = None,
        total: int | None = None,
        text: str | None = None,
        summary_patch: dict[str, Any] | None = None,
    ) -> BackgroundTaskRun:
        with get_database().atomic():
            task_run = BackgroundTaskRun.get_by_id(task_run_id)
            if current is not None:
                task_run.progress_current = int(current)
            if total is not None:
                task_run.progress_total = int(total)
            if text is not None:
                task_run.progress_text = text
            if summary_patch:
                task_run.result_summary = _merge_summary(task_run.result_summary or {}, summary_patch)
            task_run.updated_at = _now()
            task_run.save()
            SystemEventService.publish(
                event_type="task_run_updated",
                payload=ActivityService._task_run_resource(task_run).model_dump(mode="json"),
                resource_type="task_run",
                resource_id=task_run.id,
            )
            return task_run

    @staticmethod
    def _create_notification(
        *,
        category: str,
        title: str,
        content: str,
        related_task_run_id: int | None = None,
        related_resource_type: str | None = None,
        related_resource_id: int | None = None,
    ) -> SystemNotification:
        normalized_category = _normalize_allowed_filter(
            category,
            field_name="category",
            allowed_values=ALLOWED_NOTIFICATION_CATEGORIES,
        )
        with get_database().atomic():
            notification = SystemNotification.create(
                category=normalized_category or "info",
                title=title,
                content=content,
                related_task_run=related_task_run_id,
                related_resource_type=related_resource_type,
                related_resource_id=related_resource_id,
            )
            SystemEventService.publish(
                event_type="notification_created",
                payload=ActivityService._notification_resource(notification).model_dump(mode="json"),
                resource_type="notification",
                resource_id=notification.id,
            )
            return notification

    @classmethod
    def create_notification(
        cls,
        *,
        category: str,
        title: str,
        content: str,
        related_task_run_id: int | None = None,
        related_resource_type: str | None = None,
        related_resource_id: int | None = None,
    ) -> NotificationResource:
        notification = cls._create_notification(
            category=category,
            title=title,
            content=content,
            related_task_run_id=related_task_run_id,
            related_resource_type=related_resource_type,
            related_resource_id=related_resource_id,
        )
        return cls._notification_resource(notification)

    @classmethod
    def _notify_task_result(
        cls,
        task_run: BackgroundTaskRun,
        *,
        failed: bool,
    ) -> None:
        if failed:
            cls._create_notification(
                category="error",
                title=f"{task_run.task_name}执行失败",
                content=task_run.error_message or "后台任务执行失败",
                related_task_run_id=task_run.id,
            )
            return

        # 成功但带 failed/skipped 统计时升级为 warning，方便前端按分类高亮。
        category = "warning" if _detect_warning_summary(task_run.result_summary or {}) else "info"
        cls._create_notification(
            category=category,
            title=f"{task_run.task_name}已完成",
            content=task_run.result_text or "后台任务已完成",
            related_task_run_id=task_run.id,
        )

    @classmethod
    def complete_task_run(
        cls,
        task_run_id: int,
        *,
        result_summary: dict[str, Any] | None = None,
        result_text: str | None = None,
    ) -> BackgroundTaskRun:
        with get_database().atomic():
            task_run = BackgroundTaskRun.get_by_id(task_run_id)
            task_run.state = "completed"
            task_run.finished_at = _now()
            task_run.mutex_key = None
            task_run.result_summary = _merge_summary(task_run.result_summary or {}, result_summary)
            task_run.result_text = result_text or _format_result_text(task_run.result_summary)
            task_run.updated_at = _now()
            task_run.save()
            SystemEventService.publish(
                event_type="task_run_updated",
                payload=cls._task_run_resource(task_run).model_dump(mode="json"),
                resource_type="task_run",
                resource_id=task_run.id,
            )
            cls._notify_task_result(task_run, failed=False)
            return task_run

    @classmethod
    def fail_task_run(
        cls,
        task_run_id: int,
        *,
        error_message: str,
        result_summary: dict[str, Any] | None = None,
    ) -> BackgroundTaskRun:
        with get_database().atomic():
            task_run = BackgroundTaskRun.get_by_id(task_run_id)
            task_run.state = "failed"
            task_run.finished_at = _now()
            task_run.mutex_key = None
            task_run.error_message = error_message
            task_run.result_summary = _merge_summary(task_run.result_summary or {}, result_summary)
            task_run.updated_at = _now()
            task_run.save()
            SystemEventService.publish(
                event_type="task_run_updated",
                payload=cls._task_run_resource(task_run).model_dump(mode="json"),
                resource_type="task_run",
                resource_id=task_run.id,
            )
            cls._notify_task_result(task_run, failed=True)
            return task_run

    @classmethod
    def recover_task_run(
        cls,
        task_run_id: int,
        *,
        error_message: str,
        result_summary: dict[str, Any] | None = None,
        allow_null_owner: bool = False,
        force: bool = False,
    ) -> BackgroundTaskRun | None:
        task_run = BackgroundTaskRun.get_or_none(BackgroundTaskRun.id == task_run_id)
        if task_run is None:
            return None
        if task_run.state not in {"pending", "running"}:
            return None
        if not force:
            if task_run.owner_pid is None and not allow_null_owner:
                return None
            if task_run.owner_pid is not None and cls._is_process_alive(task_run.owner_pid):
                return None
        return cls.fail_task_run(
            task_run_id,
            error_message=error_message,
            result_summary=result_summary,
        )

    @classmethod
    def recover_interrupted_task_runs(
        cls,
        *,
        trigger_type: str | None = None,
        task_key: str | None = None,
        error_message: str,
        allow_null_owner: bool = False,
        force: bool = False,
    ) -> list[BackgroundTaskRun]:
        query = BackgroundTaskRun.select().where(BackgroundTaskRun.state.in_(("pending", "running")))
        if trigger_type is not None:
            query = query.where(BackgroundTaskRun.trigger_type == trigger_type)
        if task_key is not None:
            query = query.where(BackgroundTaskRun.task_key == task_key)

        recovered_task_runs: list[BackgroundTaskRun] = []
        for task_run in query.order_by(BackgroundTaskRun.id.asc()):
            recovered = cls.recover_task_run(
                task_run.id,
                error_message=error_message,
                allow_null_owner=allow_null_owner,
                force=force,
            )
            if recovered is not None:
                recovered_task_runs.append(recovered)
        return recovered_task_runs

    @staticmethod
    def find_task_run_by_mutex_key(mutex_key: str) -> BackgroundTaskRun | None:
        normalized_mutex_key = _normalize_string_filter(mutex_key)
        if normalized_mutex_key is None:
            return None
        return (
            BackgroundTaskRun.select()
            .where(BackgroundTaskRun.mutex_key == normalized_mutex_key)
            .order_by(BackgroundTaskRun.id.asc())
            .first()
        )

    @classmethod
    def create_task_reporter(
        cls,
        task_run_id: int,
        *,
        extra_callbacks: list[Callable[[dict[str, Any]], None]] | None = None,
    ) -> TaskRunReporter:
        return TaskRunReporter(
            task_run_id=task_run_id,
            summary={},
            extra_callbacks=extra_callbacks or [],
        )

    @classmethod
    def run_task(
        cls,
        *,
        task_key: str,
        trigger_type: str,
        func: Callable[[TaskRunReporter], Any],
        task_name: str | None = None,
        task_run_id: int | None = None,
        log_task_name: str | None = None,
        extra_callbacks: list[Callable[[dict[str, Any]], None]] | None = None,
        mutex_key: str | None = None,
        conflict_policy: Literal["raise", "skip"] = "raise",
    ) -> Any:
        normalized_conflict_policy = _normalize_allowed_filter(
            conflict_policy,
            field_name="conflict_policy",
            allowed_values=ALLOWED_TASK_CONFLICT_POLICIES,
        )
        normalized_mutex_key = _normalize_string_filter(mutex_key)
        # 可选的 per-task 文件日志（吸收原 runner.py 逻辑）
        if log_task_name:
            from src.scheduler.logging import get_task_logger

            task_logger = get_task_logger(log_task_name)
        else:
            task_logger = None

        ctx = logger.contextualize(task=log_task_name) if log_task_name else nullcontext()
        with ctx:
            if task_logger:
                task_logger.info("Scheduler task started")
            started_at = time.time()

            task_run = (
                BackgroundTaskRun.get_by_id(task_run_id)
                if task_run_id is not None
                else None
            )
            if task_run is None:
                try:
                    task_run = cls.create_task_run(
                        task_key=task_key,
                        task_name=task_name,
                        trigger_type=trigger_type,
                        mutex_key=normalized_mutex_key,
                    )
                except IntegrityError as exc:
                    # mutex_key 唯一约束负责跨进程互斥，冲突后再回查阻塞中的任务详情。
                    blocking_task_run = cls.find_task_run_by_mutex_key(normalized_mutex_key or "")
                    if blocking_task_run is None:
                        raise
                    if normalized_conflict_policy == "skip":
                        if task_logger:
                            task_logger.info(
                                "Scheduler task skipped by mutex conflict blocking_task_run_id={} blocking_trigger_type={}",
                                blocking_task_run.id,
                                blocking_task_run.trigger_type,
                            )
                        return _build_task_skip_result(blocking_task_run)
                    raise TaskRunConflictError(blocking_task_run) from exc
            # 统一在这里切 running，保证 APS、启动任务和线程池任务都走同一条状态链路。
            cls.mark_task_run_running(task_run.id)
            reporter = cls.create_task_reporter(task_run.id, extra_callbacks=extra_callbacks)
            context_token = cls.set_task_run_context(
                task_key=task_run.task_key,
                task_run_id=task_run.id,
                trigger_type=task_run.trigger_type,
            )
            try:
                try:
                    result = func(reporter)
                except Exception as exc:
                    cls.fail_task_run(
                        task_run.id,
                        error_message=str(exc),
                        result_summary=reporter.summary,
                    )
                    if task_logger:
                        elapsed_ms = int((time.time() - started_at) * 1000)
                        task_logger.exception("Scheduler task failed elapsed_ms={}", elapsed_ms)
                    raise

                result_summary = reporter.summary
                if isinstance(result, dict):
                    result_summary = _merge_summary(result_summary, result)
                cls.complete_task_run(
                    task_run.id,
                    result_summary=result_summary,
                )
                if task_logger:
                    elapsed_ms = int((time.time() - started_at) * 1000)
                    task_logger.info("Scheduler task finished elapsed_ms={} result={}", elapsed_ms, result)
                return result
            finally:
                cls.reset_task_run_context(context_token)

    @classmethod
    def list_notifications(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        category: str | None = None,
        is_read: bool | None = None,
        archived: bool = False,
    ) -> PageResponse[NotificationResource]:
        _validate_page(page, page_size)
        query = cls._build_notification_query(
            category=category,
            is_read=is_read,
            archived=archived,
        )
        return cls._page_notifications(query, page=page, page_size=page_size)

    @classmethod
    def get_unread_count(cls) -> NotificationUnreadCountResource:
        unread_count = (
            SystemNotification.select()
            .where(
                SystemNotification.is_read == False,
                SystemNotification.archived_at.is_null(True),
            )
            .count()
        )
        return NotificationUnreadCountResource(unread_count=unread_count)

    @classmethod
    def mark_notification_read(cls, notification_id: int) -> NotificationReadResponse:
        with get_database().atomic():
            notification = SystemNotification.get_or_none(SystemNotification.id == notification_id)
            if notification is None:
                raise ApiError(404, "notification_not_found", "通知不存在", {"notification_id": notification_id})
            if not notification.is_read:
                notification.is_read = True
                notification.read_at = _now()
                notification.updated_at = _now()
                notification.save()
                SystemEventService.publish(
                    event_type="notification_updated",
                    payload=cls._notification_resource(notification).model_dump(mode="json"),
                    resource_type="notification",
                    resource_id=notification.id,
                )
            return NotificationReadResponse(
                id=notification.id,
                is_read=notification.is_read,
                read_at=notification.read_at,
            )

    @classmethod
    def archive_notification(cls, notification_id: int) -> NotificationArchiveResponse:
        with get_database().atomic():
            notification = SystemNotification.get_or_none(SystemNotification.id == notification_id)
            if notification is None:
                raise ApiError(404, "notification_not_found", "通知不存在", {"notification_id": notification_id})
            if notification.archived_at is None:
                notification.archived_at = _now()
                notification.updated_at = _now()
                notification.save()
                SystemEventService.publish(
                    event_type="notification_updated",
                    payload=cls._notification_resource(notification).model_dump(mode="json"),
                    resource_type="notification",
                    resource_id=notification.id,
                )
            return NotificationArchiveResponse(
                id=notification.id,
                archived=notification.archived_at is not None,
                archived_at=notification.archived_at,
            )

    @classmethod
    def list_task_runs(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        state: str | None = None,
        task_key: str | None = None,
        trigger_type: str | None = None,
        sort: str | None = None,
    ) -> PageResponse[TaskRunResource]:
        _validate_page(page, page_size)
        query = cls._build_task_run_query(
            state=state,
            task_key=task_key,
            trigger_type=trigger_type,
            sort=sort,
        )
        return cls._page_task_runs(query, page=page, page_size=page_size)

    @classmethod
    def list_active_task_runs(cls) -> list[TaskRunResource]:
        query = (
            BackgroundTaskRun.select()
            .where(BackgroundTaskRun.state.in_(("pending", "running")))
            .order_by(BackgroundTaskRun.started_at.desc(), BackgroundTaskRun.id.desc())
        )
        return [cls._task_run_resource(item) for item in query]

    @classmethod
    def get_activity_bootstrap(
        cls,
        *,
        notification_category: str | None = None,
        notification_archived: bool = False,
        task_state: str | None = None,
        task_key: str | None = None,
        task_trigger_type: str | None = None,
        task_sort: str | None = None,
    ) -> ActivityBootstrapResource:
        with _activity_read_snapshot():
            # 先锁定事件游标，再读取首屏资源，保证前端后续补追的起点稳定。
            latest_event_id = (
                SystemEvent.select(fn.COALESCE(fn.MAX(SystemEvent.id), 0)).scalar()
                or 0
            )
            notifications = cls._page_notifications(
                cls._build_notification_query(
                    category=notification_category,
                    archived=notification_archived,
                ),
                page=1,
                page_size=ACTIVITY_BOOTSTRAP_PAGE_SIZE,
            )
            unread_count = cls.get_unread_count().unread_count
            active_task_runs = cls.list_active_task_runs()
            task_runs = cls._page_task_runs(
                cls._build_task_run_query(
                    state=task_state,
                    task_key=task_key,
                    trigger_type=task_trigger_type,
                    sort=task_sort,
                ),
                page=1,
                page_size=ACTIVITY_BOOTSTRAP_PAGE_SIZE,
            )
            return ActivityBootstrapResource(
                latest_event_id=int(latest_event_id),
                notifications=notifications,
                unread_count=unread_count,
                active_task_runs=active_task_runs,
                task_runs=task_runs,
            )

    @classmethod
    def create_new_media_reminder(
        cls,
        *,
        movie_items: list[dict[str, Any]],
        related_task_run_id: int | None = None,
    ) -> NotificationResource | None:
        unique_items: list[dict[str, Any]] = []
        seen_movie_numbers: set[str] = set()
        for item in movie_items:
            movie_number = str(item.get("movie_number") or "").strip()
            if not movie_number or movie_number in seen_movie_numbers:
                continue
            seen_movie_numbers.add(movie_number)
            unique_items.append(item)

        if not unique_items:
            return None

        # 提醒按批次汇总，避免导入多部影片时在通知中心刷屏。
        sample_text = "、".join(
            item.get("title") or item.get("movie_number") or ""
            for item in unique_items[:3]
        )
        if len(unique_items) > 3:
            sample_text = f"{sample_text} 等 {len(unique_items)} 部影片"
        content = f"本次后台处理新增可播放影片 {len(unique_items)} 部：{sample_text}"
        related_resource_id = unique_items[0].get("movie_id")
        notification = cls._create_notification(
            category="reminder",
            title="有新的影片可以播放了",
            content=content,
            related_task_run_id=related_task_run_id,
            related_resource_type="movie",
            related_resource_id=related_resource_id if isinstance(related_resource_id, int) else None,
        )
        return cls._notification_resource(notification)
