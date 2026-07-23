from __future__ import annotations

import time
from contextlib import nullcontext
from typing import Any, Callable, Literal

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from peewee import IntegrityError

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

            cls.mark_task_run_running(task_run.id)
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
                    cls.fail_task_run(
                        task_run.id,
                        error_message=str(exc),
                        result_summary=reporter.summary,
                        notify_result=notify_result,
                    )
                    if task_logger:
                        elapsed_ms = int((time.time() - started_at) * 1000)
                        task_logger.exception(
                            "Scheduler task failed elapsed_ms={}", elapsed_ms
                        )
                    raise

                result_summary = reporter.summary
                if isinstance(result, dict):
                    result_summary = merge_summary(result_summary, result)
                cls.complete_task_run(
                    task_run.id,
                    result_summary=result_summary,
                    notify_result=notify_result,
                )
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
