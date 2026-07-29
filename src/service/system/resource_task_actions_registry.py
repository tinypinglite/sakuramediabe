"""资源任务操作枚举与状态可用性映射（任务架构 Wave 4）。

独立小模块：state service（计算每条记录的 available_actions）与 action service
（执行操作）都依赖它，避免两者互相导入成环。action 枚举值即请求参数，前端只按
枚举渲染，不再按 task_key 硬编码按钮。
"""

from __future__ import annotations

from src.service.system.resource_task_runner import (
    STATE_EXHAUSTED,
    STATE_FAILED_RETRYABLE,
    STATE_FAILED_TERMINAL,
    STATE_SUCCEEDED,
)

ACTION_RETRY_NOW = "retry_now"
ACTION_RERUN = "rerun"
ACTION_RESET_RETRY_BUDGET = "reset_retry_budget"

SUPPORTED_ACTIONS = (ACTION_RETRY_NOW, ACTION_RERUN, ACTION_RESET_RETRY_BUDGET)

# 各 action 允许作用的投影状态（running / pending 一律不可操作）。
ACTION_ELIGIBLE_STATES: dict[str, set[str]] = {
    ACTION_RETRY_NOW: {STATE_FAILED_RETRYABLE, STATE_EXHAUSTED},
    ACTION_RERUN: {
        STATE_SUCCEEDED,
        STATE_FAILED_RETRYABLE,
        STATE_FAILED_TERMINAL,
        STATE_EXHAUSTED,
    },
    ACTION_RESET_RETRY_BUDGET: {
        STATE_FAILED_RETRYABLE,
        STATE_FAILED_TERMINAL,
        STATE_EXHAUSTED,
    },
}


def available_actions_for_state(state: str) -> list[str]:
    """单条投影记录当前可用的 action 列表，随记录返回给前端渲染。"""
    return [
        action
        for action in SUPPORTED_ACTIONS
        if state in ACTION_ELIGIBLE_STATES[action]
    ]
