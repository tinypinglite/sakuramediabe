from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any, Literal

from loguru import logger
from peewee import IntegrityError
from pydantic import BaseModel, ConfigDict, Field

from src.model import BackgroundTaskRun
from src.service.system.activity.filters import (
    normalize_allowed_filter,
    normalize_string_filter,
)
from src.service.system.activity.task_runs import merge_summary

ALLOWED_TASK_CONFLICT_POLICIES = {"raise", "skip"}


class TaskRunConflictError(RuntimeError):
    def __init__(self, blocking_task_run: BackgroundTaskRun):
        self.blocking_task_run = blocking_task_run
        super().__init__(self.format_message(blocking_task_run))

    @staticmethod
    def format_message(task_run: BackgroundTaskRun) -> str:
        started_at_text = (
            task_run.started_at.isoformat(sep=" ", timespec="seconds")
            if task_run.started_at
            else "未知"
        )
        return (
            f"任务“{task_run.task_name}”已在运行中，"
            f"trigger_type={task_run.trigger_type} task_run_id={task_run.id} "
            f"started_at={started_at_text}"
        )


class TaskRunFinalizedError(RuntimeError):
    """当前执行器未赢得 TaskRun 状态转移，持久终态是唯一可信结果。"""

    def __init__(self, task_run: BackgroundTaskRun):
        self.task_run = task_run
        detail = task_run.error_message or task_run.result_text or "任务已由其它执行器收口"
        super().__init__(
            f"task_run 已终态化 task_run_id={task_run.id} "
            f"state={task_run.state} detail={detail}"
        )


def _build_task_skip_result(blocking_task_run: BackgroundTaskRun) -> dict[str, Any]:
    return {
        "task_skipped": True,
        "reason": "mutex_conflict",
        "blocking_task_run_id": blocking_task_run.id,
        "blocking_task_key": blocking_task_run.task_key,
        "blocking_trigger_type": blocking_task_run.trigger_type,
        "blocking_started_at": (
            blocking_task_run.started_at.isoformat()
            if blocking_task_run.started_at
            else None
        ),
        "blocking_task_name": blocking_task_run.task_name,
    }


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
            self.summary = merge_summary(self.summary, summary_patch)
        # 延迟引用兼容门面，保留既有 monkeypatch 路径。
        from src.service.system.activity.facade import ActivityService

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
        for callback in self.extra_callbacks:
            callback(payload)


class TaskExecutionService:
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
        notify_result: bool = True,
    ) -> Any:
        normalized_conflict_policy = normalize_allowed_filter(
            conflict_policy,
            field_name="conflict_policy",
            allowed_values=ALLOWED_TASK_CONFLICT_POLICIES,
        )
        normalized_mutex_key = normalize_string_filter(mutex_key)
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
                    blocking_task_run = cls.find_task_run_by_mutex_key(
                        normalized_mutex_key or ""
                    )
                    if blocking_task_run is None:
                        raise
                    if normalized_conflict_policy == "skip":
                        if task_logger:
                            task_logger.info(
                                "Scheduler task skipped by mutex conflict "
                                "blocking_task_run_id={} blocking_trigger_type={}",
                                blocking_task_run.id,
                                blocking_task_run.trigger_type,
                            )
                        return _build_task_skip_result(blocking_task_run)
                    raise TaskRunConflictError(blocking_task_run) from exc

            task_run = cls.mark_task_run_running(task_run.id)
            if task_run.state != "running":
                # terminal run 绝不能再次进入插件/领域执行体。队列 worker 传入的行已由
                # claim_next 置 running，因此 running 原样返回仍是合法的预领取执行。
                raise TaskRunFinalizedError(task_run)
            reporter = cls.create_task_reporter(
                task_run.id, extra_callbacks=extra_callbacks
            )
            context_token = cls.set_task_run_context(
                task_key=task_run.task_key,
                task_run_id=task_run.id,
                trigger_type=task_run.trigger_type,
            )
            try:
                try:
                    result = func(reporter)
                except Exception as exc:
                    failed_run, transitioned = cls._fail_task_run_transition(
                        task_run.id,
                        error_message=str(exc),
                        result_summary=reporter.summary,
                        notify_result=notify_result,
                    )
                    if not transitioned:
                        if failed_run.state == "completed":
                            # 本执行体虽抛错，但另一路已经成功收口；调用者必须观察持久
                            # completed，而不是继续传播这条迟到异常。
                            if task_logger:
                                task_logger.warning(
                                    "Scheduler task exception ignored after persisted completion"
                                )
                            return dict(failed_run.result_summary or {})
                        raise TaskRunFinalizedError(failed_run) from exc
                    if task_logger:
                        elapsed_ms = int((time.time() - started_at) * 1000)
                        task_logger.exception(
                            "Scheduler task failed elapsed_ms={}", elapsed_ms
                        )
                    raise

                result_summary = reporter.summary
                if isinstance(result, dict):
                    result_summary = merge_summary(result_summary, result)
                completed_run, transitioned = cls._complete_task_run_transition(
                    task_run.id,
                    result_summary=result_summary,
                    notify_result=notify_result,
                )
                if not transitioned:
                    if completed_run.state == "failed":
                        # 本地执行成功也不能覆盖已持久化的失败终态。
                        raise TaskRunFinalizedError(completed_run)
                    # 另一执行器已先完成时，以数据库 summary 作为可观察结果。
                    return dict(completed_run.result_summary or {})
                if task_logger:
                    elapsed_ms = int((time.time() - started_at) * 1000)
                    task_logger.info(
                        "Scheduler task finished elapsed_ms={} result={}",
                        elapsed_ms,
                        result,
                    )
                return result
            finally:
                cls.reset_task_run_context(context_token)
