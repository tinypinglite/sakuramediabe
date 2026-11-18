"""任务 result_summary 的语义判定，供通知与任务运行出参共用。"""

from __future__ import annotations


def has_failed_summary(summary: dict | None) -> bool:
    """summary 是否存在大于 0 的 failed 计数（键名含 failed 的数值项）。

    通知 warning 与 TaskRunResource.has_warning 必须保持同一判定；两处各写一遍
    会让「通知发了 warning、列表却不算告警」这类漂移长期隐性存在。
    """
    if not summary:
        return False
    for key, value in summary.items():
        if isinstance(value, (int, float)) and value > 0 and "failed" in key:
            return True
    return False
