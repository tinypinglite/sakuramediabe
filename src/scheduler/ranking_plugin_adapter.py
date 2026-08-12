"""插件扩展点 → 宿主排行榜注册表转换（scheduler 层）。

转换放这里而不是 service 层：保持 ``api -> service -> model`` 依赖方向，
service 只收宿主自己的 ``RankingSourceDefinition``，不依赖插件契约。
"""

from __future__ import annotations

from src.plugins.contracts import PluginRegistration
from src.plugins.extensions.ranking import (
    RANKING_SOURCE_EXTENSION_KEY,
    PluginRankingSource,
    validate_ranking_extension,
)
from src.plugins.loader import PLUGIN_LOAD_ERRORS
from src.service.discovery.ranking_service import (
    RankingBoardDefinition,
    RankingSourceDefinition,
    register_plugin_ranking_sources,
)


def _to_definition(
    source: PluginRankingSource,
    plugin_id: str,
) -> RankingSourceDefinition:
    """把插件声明转成宿主数据结构；回调原样保留。"""
    return RankingSourceDefinition(
        key=source.source_key,
        name=source.name,
        plugin_id=plugin_id,
        boards=tuple(
            RankingBoardDefinition(
                key=board.key,
                name=board.name,
                supported_periods=board.supported_periods,
                default_period=board.default_period,
                requires_account=board.requires_account,
                should_fetch=board.should_fetch,
                supported_periods_provider=board.supported_periods_provider,
                fetch_numbers=board.fetch_numbers,
            )
            for board in source.boards
        ),
    )


def apply_plugin_ranking_sources(
    registrations: tuple[PluginRegistration, ...],
) -> set[str]:
    """把插件扩展点声明合并进排行榜注册表；返回被冲突隔离的插件 ID 集合。

    只收集 ``key == discovery.ranking_source`` 的扩展点；任一 ``source_key``
    与已接受者冲突时，该插件**整插件拒绝**（来源与任务都不注册），
    行为与任务注册表冲突语义一致。
    """
    accepted: list[RankingSourceDefinition] = []
    owners: dict[str, str] = {}
    claimed: set[str] = set()
    rejected: set[str] = set()

    for registration in registrations:
        pending: list[PluginRankingSource] = []
        pending_keys: set[str] = set()
        conflict = False
        for extension in registration.extensions:
            if extension.key != RANKING_SOURCE_EXTENSION_KEY:
                continue
            try:
                source = validate_ranking_extension(
                    plugin_id=registration.plugin_id,
                    extension=extension,
                )
            except Exception as exc:
                PLUGIN_LOAD_ERRORS[registration.plugin_id] = {
                    "stage": "ranking_extension",
                    "message": str(exc),
                }
                rejected.add(registration.plugin_id)
                conflict = True
                break
            if source.source_key in pending_keys:
                PLUGIN_LOAD_ERRORS[registration.plugin_id] = {
                    "stage": "ranking_extension",
                    "message": f"插件内部 source_key 重复: {source.source_key}",
                }
                rejected.add(registration.plugin_id)
                conflict = True
                break
            pending_keys.add(source.source_key)
            if source.source_key in claimed:
                PLUGIN_LOAD_ERRORS[registration.plugin_id] = {
                    "stage": "ranking_conflict",
                    "message": (
                        f"排行榜来源冲突 source_key={source.source_key} "
                        f"已被其它插件占用"
                    ),
                }
                rejected.add(registration.plugin_id)
                conflict = True
                break
            pending.append(source)
        if conflict:
            continue
        for source in pending:
            claimed.add(source.source_key)
            owners[source.source_key] = registration.plugin_id
            accepted.append(_to_definition(source, registration.plugin_id))

    register_plugin_ranking_sources(accepted, owners)
    return rejected
