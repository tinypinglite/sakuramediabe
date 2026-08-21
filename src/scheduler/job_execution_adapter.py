"""宿主内部的后台任务执行适配器。

``JobDefinition`` 是插件公开契约，``QueueTaskDefinition`` 是宿主私有执行覆盖；
本模块只负责把两种声明解析成 worker 可调用的统一形态，不改变任一声明对象。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.scheduler.contracts import JobDefinition
from src.scheduler.queue_tasks import QueueTaskDefinition


class JobExecutionResolutionError(RuntimeError):
    """当前任务声明与持久参数无法组成合法执行体。"""


@dataclass(frozen=True)
class ResolvedJobExecution:
    """worker 实际执行所需的宿主内部描述。"""

    func: Callable[[Any], Any]
    log_name: str
    notify_result: bool


def resolve_job_definition_handler(
    *,
    job_def: JobDefinition,
    raw_params: dict[str, Any] | None,
) -> Callable[[Any], Any]:
    """只按 ``JobDefinition`` 解析执行体，供同步旧入口与 worker 共用。

    该 helper 不感知 ``QueueTaskDefinition``，因此同步 CLI/run_job 不会意外执行
    宿主的 queue override。显式参数对象（包括 ``{}``）只能交给
    ``params_handler``；数据库 NULL 只能交给 ``service_factory``。
    """
    has_params = raw_params is not None
    if has_params and not isinstance(raw_params, dict):
        raise JobExecutionResolutionError(
            f"任务参数必须是 JSON object task_key={job_def.task_key}"
        )

    if has_params:
        params_handler = job_def.params_handler
        if params_handler is None:
            raise JobExecutionResolutionError(
                f"任务不支持带参执行 task_key={job_def.task_key}"
            )
        params = raw_params

        def run_params_handler(reporter):
            return params_handler(reporter, params)

        return run_params_handler

    service_factory = job_def.service_factory
    if service_factory is not None:
        return service_factory
    raise JobExecutionResolutionError(f"任务缺少无参执行体 task_key={job_def.task_key}")


def resolve_job_execution(
    *,
    task_key: str,
    raw_params: dict[str, Any] | None,
    job_def: JobDefinition | None,
    queue_def: QueueTaskDefinition | None,
) -> ResolvedJobExecution:
    """按持久参数是否为 NULL 解析唯一执行体。

    ``NULL`` 表示无参调度，走 ``service_factory``；非 NULL（包括空对象
    ``{}``）表示一次显式参数调用。宿主同 key 的 queue override 对显式参数保持
    最高优先级，队列专属 key 则始终由 queue handler 执行。
    """
    # unknown 无论携带何种 params 都必须稳定报“未注册”，不能伪装成参数错误。
    if job_def is None and queue_def is None:
        raise JobExecutionResolutionError(f"task_key 未在注册表中: {task_key}")

    has_params = raw_params is not None
    if has_params and not isinstance(raw_params, dict):
        raise JobExecutionResolutionError(
            f"任务参数必须是 JSON object task_key={task_key}"
        )

    if queue_def is not None and (job_def is None or has_params):
        params = raw_params if raw_params is not None else {}

        def run_queue_handler(reporter):
            return queue_def.handler(reporter, params)

        return ResolvedJobExecution(
            func=run_queue_handler,
            log_name=queue_def.log_name,
            notify_result=queue_def.notify_result,
        )

    if job_def is not None:
        return ResolvedJobExecution(
            func=resolve_job_definition_handler(
                job_def=job_def,
                raw_params=raw_params,
            ),
            log_name=job_def.log_name,
            notify_result=True,
        )

    # 前面的 queue 分支已覆盖 queue-only，走到这里说明声明组合不完整。
    raise JobExecutionResolutionError(f"任务缺少执行体 task_key={task_key}")
