from fastapi import APIRouter, Depends

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.i18n.text import resolve_text
from src.plugins.manager import PluginManager
from src.schema.system.plugins import (
    MetadataSourceOrderRequest,
    MetadataSourceOrderResource,
    MetadataSourceResource,
)
from src.service.catalog.metadata_source_service import MetadataSourceService

router = APIRouter(
    prefix="/system/metadata-sources",
    tags=["metadata-sources"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


def _items() -> list[MetadataSourceResource]:
    return [
        MetadataSourceResource(
            plugin_id=plugin_id,
            display_name=resolve_text(name, name_i18n),
        )
        for plugin_id, name, name_i18n, _source in MetadataSourceService.enabled_plugin_sources()
    ]


@router.get("", response_model=list[MetadataSourceResource])
def list_metadata_sources():
    return _items()


@router.put("/order", response_model=MetadataSourceOrderResource)
def reorder_metadata_sources(payload: MetadataSourceOrderRequest):
    metadata_ids = {
        plugin_id
        for plugin_id, _name, _name_i18n, _source in (
            MetadataSourceService.enabled_plugin_sources()
        )
    }
    try:
        PluginManager().reorder_enabled_metadata_sources(
            plugin_ids=payload.plugin_ids,
            metadata_plugin_ids=metadata_ids,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_metadata_source_order", str(exc)) from exc
    return MetadataSourceOrderResource(
        items=_items(),
        pending_restart=["api", "aps"],
    )
