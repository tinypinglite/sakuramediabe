"""选种策略的内置默认值与初始化。"""

from __future__ import annotations

from src.model import DownloadSelectionPolicy
from src.schema.transfers.download_selection import (
    GIB,
    DownloadSelectionRanking,
    DownloadSelectionRules,
)

MIN_SIZE_BYTES = 1 * GIB
MAX_SIZE_BYTES = 40 * GIB
MAX_REJECTED_CANDIDATES = 5


def legacy_selection_rules() -> DownloadSelectionRules:
    """严格复刻引入选种策略前的自动下载行为。"""

    return DownloadSelectionRules(
        min_size_bytes=MIN_SIZE_BYTES,
        max_size_bytes=MAX_SIZE_BYTES,
        min_seeders=1,
        ranking=[DownloadSelectionRanking.SIZE_DESC],
    )


def new_user_selection_rules() -> DownloadSelectionRules:
    """新安装默认：体积优先，再做种数与发布时间稳定排序。"""

    return DownloadSelectionRules(
        min_size_bytes=MIN_SIZE_BYTES,
        max_size_bytes=MAX_SIZE_BYTES,
        min_seeders=1,
        ranking=[
            DownloadSelectionRanking.SIZE_DESC,
            DownloadSelectionRanking.SEEDERS_DESC,
            DownloadSelectionRanking.PUBLISHED_AT_DESC,
        ],
    )


def default_selection_rules(*, fresh_install: bool) -> DownloadSelectionRules:
    return new_user_selection_rules() if fresh_install else legacy_selection_rules()


def seed_default_selection_policy(
    database,
    *,
    fresh_install: bool,
) -> bool:
    """策略表为空时补入对应默认值；已有策略永不覆盖。"""

    with database.atomic():
        if DownloadSelectionPolicy.select(DownloadSelectionPolicy.id).where(
            DownloadSelectionPolicy.id == 1
        ).exists():
            return False
        DownloadSelectionPolicy.create(
            id=1,
            rules=default_selection_rules(fresh_install=fresh_install).model_dump(
                mode="json"
            ),
            version=1,
        )
        return True
