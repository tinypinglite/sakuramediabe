from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


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
