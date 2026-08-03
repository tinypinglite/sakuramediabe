from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TaskRunContext:
    task_key: str
    task_run_id: int
    trigger_type: str


TASK_RUN_CONTEXT: ContextVar[TaskRunContext | None] = ContextVar(
    "task_run_context", default=None
)


class TaskRunContextService:
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
        # 线程池不会自动继承 ContextVar，提交任务时显式复制上下文。
        runtime_context = copy_context()

        def _run_with_context(*args, **kwargs):
            return runtime_context.run(func, *args, **kwargs)

        return _run_with_context
