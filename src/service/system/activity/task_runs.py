from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from src.api.exception.errors import ApiError
from src.common.process import is_process_alive
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import validate_page
from src.model import BackgroundTaskRun
from src.model.base import get_database
from src.schema.common.pagination import PageResponse
from src.schema.system.activity import TaskRunResource
from src.service.system.activity.filters import (
    normalize_allowed_filter,
    normalize_string_filter,
)
from src.service.system.activity.notifications import NotificationService
from src.service.system.activity.task_catalog import TASK_NAME_REGISTRY

ALLOWED_TASK_TRIGGER_TYPES = {"scheduled", "manual", "startup", "internal"}
ALLOWED_TASK_STATES = {"pending", "running", "completed", "failed"}
ACTIVE_TASK_RUN_STATES = {"pending", "running"}
TASK_RUN_SORT_FIELDS = {
    "started_at:desc": (BackgroundTaskRun.started_at.desc(), BackgroundTaskRun.id.desc()),
    "started_at:asc": (BackgroundTaskRun.started_at.asc(), BackgroundTaskRun.id.asc()),
    "created_at:desc": (BackgroundTaskRun.created_at.desc(), BackgroundTaskRun.id.desc()),
    "created_at:asc": (BackgroundTaskRun.created_at.asc(), BackgroundTaskRun.id.asc()),
    "updated_at:desc": (BackgroundTaskRun.updated_at.desc(), BackgroundTaskRun.id.desc()),
    "updated_at:asc": (BackgroundTaskRun.updated_at.asc(), BackgroundTaskRun.id.asc()),
}


# 序列化/查询入口带 task_run 前缀是刻意的：ActivityService 门面把本类与
# NotificationService 多继承在一起，base 之间一旦出现同名方法，cls.xxx 就会被 MRO
# 静默解析到另一侧。名字不撞则 cls. 自引用天然安全，护栏见
# tests/test_architecture_boundaries.py。


def now() -> datetime:
    return utc_now_for_db()


def merge_summary(base_summary: dict[str, Any], summary_patch: dict[str, Any] | None) -> dict[str, Any]:
    if not summary_patch:
        return dict(base_summary)
    merged = dict(base_summary)
    merged.update(summary_patch)
    return merged


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def format_result_text(summary: dict[str, Any] | None) -> str | None:
    if not summary:
        return None
    fragments: list[str] = []
    for key, value in summary.items():
        if isinstance(value, (dict, list)) or value is None:
            continue
        fragments.append(f"{key}={_format_scalar(value)}")
    return " ".join(fragments) if fragments else None


class TaskRunService:
    @staticmethod
    def _lock_task_run(task_run_id: int) -> BackgroundTaskRun:
        """锁定并重读 task_run，供状态转移在同一事务内裁决唯一赢家。"""
        return (
            BackgroundTaskRun.select()
            .where(BackgroundTaskRun.id == task_run_id)
            .for_update()
            .get()
        )

    @staticmethod
    def to_task_run_resource(task_run: BackgroundTaskRun) -> TaskRunResource:
        return TaskRunResource.model_validate(task_run)

    @staticmethod
    def resolve_task_name(task_key: str, task_name: str | None = None) -> str:
        return (task_name or "").strip() or TASK_NAME_REGISTRY.get(task_key, task_key)

    @classmethod
    def build_task_run_query(
        cls,
        *,
        state: str | None = None,
        task_key: str | None = None,
        trigger_type: str | None = None,
        sort: str | None = None,
    ):
        normalized_state = normalize_allowed_filter(
            state, field_name="state", allowed_values=ALLOWED_TASK_STATES
        )
        normalized_trigger_type = normalize_allowed_filter(
            trigger_type,
            field_name="trigger_type",
            allowed_values=ALLOWED_TASK_TRIGGER_TYPES,
        )
        normalized_task_key = normalize_string_filter(task_key)
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
    def page_task_runs(cls, query, *, page: int, page_size: int) -> PageResponse[TaskRunResource]:
        total = query.count()
        start = (page - 1) * page_size
        items = [cls.to_task_run_resource(item) for item in query.offset(start).limit(page_size)]
        return PageResponse[TaskRunResource](
            items=items, page=page, page_size=page_size, total=total
        )

    @classmethod
    def create_task_run(
        cls,
        *,
        task_key: str,
        task_name: str | None = None,
        trigger_type: str,
        state: str = "pending",
        owner_pid: int | None = None,
        mutex_key: str | None = None,
        params: dict[str, Any] | None = None,
        scheduled_at: datetime | None = None,
    ) -> BackgroundTaskRun:
        normalized_trigger_type = normalize_allowed_filter(
            trigger_type,
            field_name="trigger_type",
            allowed_values=ALLOWED_TASK_TRIGGER_TYPES,
        )
        normalized_state = normalize_allowed_filter(
            state, field_name="state", allowed_values=ALLOWED_TASK_STATES
        )
        with get_database().atomic():
            task_run = BackgroundTaskRun.create(
                task_key=task_key,
                task_name=cls.resolve_task_name(task_key, task_name),
                trigger_type=normalized_trigger_type or "internal",
                owner_pid=os.getpid() if owner_pid is None else owner_pid,
                mutex_key=normalize_string_filter(mutex_key),
                state=normalized_state or "pending",
                started_at=now() if normalized_state == "running" else None,
                result_summary={},
                # scheduled_at 非空是"队列托管行"的判别标志（TaskQueueService 领取范围）；
                # 进程内直跑的 task_run 保持为空，不会被 worker 抢走。
                params=params,
                scheduled_at=scheduled_at,
            )
            return task_run

    @classmethod
    def mark_task_run_running(cls, task_run_id: int) -> BackgroundTaskRun:
        """仅执行 pending -> running；其它状态原样返回且不产生任何写入。"""
        with get_database().atomic():
            task_run = cls._lock_task_run(task_run_id)
            if task_run.state != "pending":
                return task_run
            task_run.state = "running"
            if task_run.started_at is None:
                task_run.started_at = now()
            task_run.updated_at = now()
            task_run.save()
            return task_run

    @classmethod
    def update_task_run_progress(
        cls,
        task_run_id: int,
        *,
        current: int | None = None,
        total: int | None = None,
        text: str | None = None,
        summary_patch: dict[str, Any] | None = None,
    ) -> BackgroundTaskRun:
        """仅更新 active run 的显式进度字段；终态行原样返回且零写入。

        返回值始终是锁内最新持久行。未发生更新时仍保持既有返回类型，调用方可从
        ``state`` 判断任务已经终态化；实现不对模型实例做整行 ``save``，避免旧快照
        把 state / mutex / finished_at 等并发终态字段写回。
        """
        with get_database().atomic():
            task_run = cls._lock_task_run(task_run_id)
            if task_run.state not in ACTIVE_TASK_RUN_STATES:
                return task_run
            updates: dict[Any, Any] = {BackgroundTaskRun.updated_at: now()}
            if current is not None:
                updates[BackgroundTaskRun.progress_current] = int(current)
            if total is not None:
                updates[BackgroundTaskRun.progress_total] = int(total)
            if text is not None:
                updates[BackgroundTaskRun.progress_text] = text
            if summary_patch:
                updates[BackgroundTaskRun.result_summary] = merge_summary(
                    task_run.result_summary or {}, summary_patch
                )
            # 行锁保证 summary 基于最新值合并；WHERE active 是数据库侧的最终护栏。
            updated_count = (
                BackgroundTaskRun.update(updates)
                .where(
                    BackgroundTaskRun.id == task_run_id,
                    BackgroundTaskRun.state.in_(ACTIVE_TASK_RUN_STATES),
                )
                .execute()
            )
            if updated_count != 1:
                return BackgroundTaskRun.get_by_id(task_run_id)
            return BackgroundTaskRun.get_by_id(task_run_id)

    @classmethod
    def complete_task_run(
        cls,
        task_run_id: int,
        *,
        result_summary: dict[str, Any] | None = None,
        result_text: str | None = None,
        notify_result: bool = True,
    ) -> BackgroundTaskRun:
        """把 active run 原子收口为 completed。

        若行已是任一终态，返回锁内读到的当前行，不覆盖终态、summary、mutex，
        也不重复发送通知；保留既有 ``BackgroundTaskRun`` 返回类型。
        """
        task_run, _transitioned = cls._complete_task_run_transition(
            task_run_id,
            result_summary=result_summary,
            result_text=result_text,
            notify_result=notify_result,
        )
        return task_run

    @classmethod
    def _complete_task_run_transition(
        cls,
        task_run_id: int,
        *,
        result_summary: dict[str, Any] | None = None,
        result_text: str | None = None,
        notify_result: bool = True,
    ) -> tuple[BackgroundTaskRun, bool]:
        """完成终态 CAS 的内部结果；bool 只供执行器判断自己是否赢得转移。"""
        with get_database().atomic():
            task_run = cls._lock_task_run(task_run_id)
            if task_run.state not in ACTIVE_TASK_RUN_STATES:
                return task_run, False
            task_run.state = "completed"
            task_run.finished_at = now()
            task_run.mutex_key = None
            task_run.lease_expires_at = None
            task_run.result_summary = merge_summary(task_run.result_summary or {}, result_summary)
            task_run.result_text = result_text or format_result_text(task_run.result_summary)
            task_run.updated_at = now()
            task_run.save()
            if notify_result:
                NotificationService.notify_task_result(task_run, failed=False)
            return task_run, True

    @classmethod
    def fail_task_run(
        cls,
        task_run_id: int,
        *,
        error_message: str,
        result_summary: dict[str, Any] | None = None,
        notify_result: bool = True,
    ) -> BackgroundTaskRun:
        """把 active run 原子收口为 failed；已终态时只返回当前持久行。"""
        task_run, _transitioned = cls._fail_task_run_transition(
            task_run_id,
            error_message=error_message,
            result_summary=result_summary,
            notify_result=notify_result,
        )
        return task_run

    @classmethod
    def _fail_task_run_transition(
        cls,
        task_run_id: int,
        *,
        error_message: str,
        result_summary: dict[str, Any] | None = None,
        notify_result: bool = True,
    ) -> tuple[BackgroundTaskRun, bool]:
        """失败终态 CAS 的内部结果；bool 只供执行器判断自己是否赢得转移。"""
        with get_database().atomic():
            task_run = cls._lock_task_run(task_run_id)
            transitioned = cls._fail_locked_task_run(
                task_run,
                error_message=error_message,
                result_summary=result_summary,
                notify_result=notify_result,
            )
            return task_run, transitioned

    @staticmethod
    def _fail_locked_task_run(
        task_run: BackgroundTaskRun,
        *,
        error_message: str,
        result_summary: dict[str, Any] | None,
        notify_result: bool,
    ) -> bool:
        """在调用方已持有行锁时尝试失败收口，返回是否真正完成状态转移。"""
        if task_run.state not in ACTIVE_TASK_RUN_STATES:
            return False
        task_run.state = "failed"
        task_run.finished_at = now()
        task_run.mutex_key = None
        task_run.lease_expires_at = None
        task_run.error_message = error_message
        task_run.result_summary = merge_summary(
            task_run.result_summary or {}, result_summary
        )
        task_run.updated_at = now()
        task_run.save()
        if notify_result:
            NotificationService.notify_task_result(task_run, failed=True)
        return True

    @classmethod
    def recover_task_run(
        cls,
        task_run_id: int,
        *,
        error_message: str,
        result_summary: dict[str, Any] | None = None,
        allow_null_owner: bool = False,
        force: bool = False,
        notify_result: bool = True,
    ) -> BackgroundTaskRun | None:
        with get_database().atomic():
            task_run = BackgroundTaskRun.get_or_none(
                BackgroundTaskRun.id == task_run_id
            )
            if task_run is None:
                return None
            task_run = cls._lock_task_run(task_run.id)
            if task_run.state not in ACTIVE_TASK_RUN_STATES:
                return None
            if not force:
                if task_run.owner_pid is None and not allow_null_owner:
                    return None
                if task_run.owner_pid is not None and is_process_alive(
                    task_run.owner_pid
                ):
                    return None
            transitioned = cls._fail_locked_task_run(
                task_run,
                error_message=error_message,
                result_summary=result_summary,
                notify_result=notify_result,
            )
            return task_run if transitioned else None

    @classmethod
    def recover_interrupted_task_runs(
        cls,
        *,
        trigger_type: str | None = None,
        task_key: str | None = None,
        error_message: str,
        allow_null_owner: bool = False,
        force: bool = False,
        suppress_notification_task_keys: set[str] | None = None,
    ) -> list[BackgroundTaskRun]:
        query = BackgroundTaskRun.select().where(
            BackgroundTaskRun.state.in_(("pending", "running")),
            # 队列托管行（scheduled_at 非空）不走 owner_pid 判活回收：pending 行本就该
            # 跨进程重启存活，running 行由 TaskQueueService 的租约过期机制负责。
            BackgroundTaskRun.scheduled_at.is_null(True),
        )
        if trigger_type is not None:
            query = query.where(BackgroundTaskRun.trigger_type == trigger_type)
        if task_key is not None:
            query = query.where(BackgroundTaskRun.task_key == task_key)
        suppressed_task_keys = suppress_notification_task_keys or set()
        recovered_task_runs: list[BackgroundTaskRun] = []
        for task_run in query.order_by(BackgroundTaskRun.id.asc()):
            recovered = cls.recover_task_run(
                task_run.id,
                error_message=error_message,
                allow_null_owner=allow_null_owner,
                force=force,
                notify_result=task_run.task_key not in suppressed_task_keys,
            )
            if recovered is not None:
                recovered_task_runs.append(recovered)
        return recovered_task_runs

    @staticmethod
    def find_task_run_by_mutex_key(mutex_key: str) -> BackgroundTaskRun | None:
        normalized_mutex_key = normalize_string_filter(mutex_key)
        if normalized_mutex_key is None:
            return None
        return (
            BackgroundTaskRun.select()
            .where(BackgroundTaskRun.mutex_key == normalized_mutex_key)
            .order_by(BackgroundTaskRun.id.asc())
            .first()
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
        validate_page(page, page_size, error_code="invalid_pagination")
        return cls.page_task_runs(
            cls.build_task_run_query(
                state=state,
                task_key=task_key,
                trigger_type=trigger_type,
                sort=sort,
            ),
            page=page,
            page_size=page_size,
        )

    @classmethod
    def get_task_run_resource(cls, task_run_id: int) -> TaskRunResource:
        """单条详情：202 入队后前端仅凭 task_run_id 追溯终态与错误信息的通路。"""
        task_run = BackgroundTaskRun.get_or_none(BackgroundTaskRun.id == task_run_id)
        if task_run is None:
            raise ApiError(
                404,
                "task_run_not_found",
                "任务运行记录不存在或已被清理",
                {"task_run_id": task_run_id},
            )
        return cls.to_task_run_resource(task_run)

    @classmethod
    def list_active_task_runs(cls) -> list[TaskRunResource]:
        query = (
            BackgroundTaskRun.select()
            .where(BackgroundTaskRun.state.in_(("pending", "running")))
            .order_by(BackgroundTaskRun.started_at.desc(), BackgroundTaskRun.id.desc())
        )
        return [cls.to_task_run_resource(item) for item in query]
