"""资源任务操作枚举与状态可用性映射（任务架构 Wave 4）。

独立小模块：state service（计算每条记录的 available_actions）与 action service
（执行操作）都依赖它，避免两者互相导入成环。action 枚举值即请求参数，前端只按
枚举渲染，不再按 task_key 硬编码按钮。
"""

from __future__ import annotations

from collections.abc import Sequence

from src.service.system.resource_task_runner import (
    STATE_EXHAUSTED,
    STATE_FAILED_RETRYABLE,
    STATE_FAILED_TERMINAL,
    STATE_PENDING,
    STATE_SUCCEEDED,
)

ACTION_RETRY_NOW = "retry_now"
ACTION_RERUN = "rerun"
ACTION_RESET_RETRY_BUDGET = "reset_retry_budget"

SUPPORTED_ACTIONS = (ACTION_RETRY_NOW, ACTION_RERUN, ACTION_RESET_RETRY_BUDGET)

# 各 action 允许作用的投影状态（running 一律不可操作）。
# rerun 是通用"立即强制执行"入口：pending（含从未记账被播种的行）也可操作，
# 对应场景是等不及下一轮 cron、或此前 run 空转留下的滞留行。
ACTION_ELIGIBLE_STATES: dict[str, set[str]] = {
    ACTION_RETRY_NOW: {STATE_FAILED_RETRYABLE, STATE_EXHAUSTED},
    ACTION_RERUN: {
        STATE_PENDING,
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


def available_actions_for_state(state: str, supported: Sequence[str] = SUPPORTED_ACTIONS) -> list[str]:
    """单条投影记录当前可用的 action 列表 = 状态可用集 ∩ 任务声明支持集。

    supported 来自 ResourceTaskDefinition.supported_actions：rerun（强制重跑）
    只有域语义安全的任务开放——订阅查询 rerun 会给已有媒体的影片重复提交下载、
    缩略图不支持覆盖再生，这两类任务不声明 rerun。
    """
    return [
        action
        for action in SUPPORTED_ACTIONS
        if action in supported and state in ACTION_ELIGIBLE_STATES[action]
    ]
