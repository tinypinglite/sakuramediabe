"""插件管理 API：zip 上传安装、启停、移除与状态查询。

插件在 api/aps 的 import 阶段加载。声明 dependencies 的插件需要完整容器启动，
在加载前同步依赖；其他插件仍只需重启 api 与 aps。安装 = 上传 zip 并发布目录，
目标已存在时替换代码并保留 data/。
"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, Request, UploadFile

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.i18n import translate
from src.i18n.text import resolve_text
from src.plugins.installer import MAX_ARCHIVE_BYTES
from src.plugins.manager import PluginManager, PluginSettingsValidationError
from src.schema.system.plugins import (
    PluginBindingResource,
    PluginBindingSourceResource,
    PluginBindingUpdateRequest,
    PluginBindingUpdateResource,
    PluginDetailResource,
    PluginInstallResponse,
    PluginSettingsResource,
    PluginSettingsUpdateResource,
    PluginSummaryResource,
    PluginTestRequest,
    PluginTestResultResource,
    StatsFieldResource,
)
from src.service.system.plugin_binding_service import PluginBindingService
from src.service.system.plugin_removal_service import (
    PluginBindingInUseError,
    PluginInUseError,
    PluginRemovalService,
)
from src.service.system.plugin_test_service import (
    PluginTestCapabilityUnsupported,
    PluginTestService,
    PluginTestUnavailable,
)

router = APIRouter(
    prefix="/system/plugins",
    tags=["plugins"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)

_UPLOAD_STALE_SECONDS = 24 * 3600


def _check_upload_size(request: Request) -> None:
    """上传前先按 Content-Length 拦截超限包，避免大文件先落盘再被拒。"""
    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > MAX_ARCHIVE_BYTES
    ):
        raise ApiError(
            413,
            "plugin_too_large",
            f"插件包超过大小上限 {MAX_ARCHIVE_BYTES} 字节",
        )


def _upload_temp_path(manager: PluginManager) -> Path:
    upload_dir = manager.root_dir / ".staging" / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 清理上次异常退出残留的临时上传，避免 .staging/uploads 无限累积。
    now = time.time()
    for entry in upload_dir.iterdir():
        try:
            if entry.is_file() and now - entry.stat().st_mtime > _UPLOAD_STALE_SECONDS:
                entry.unlink(missing_ok=True)
        except OSError:
            continue
    return upload_dir / f"{uuid.uuid4().hex}.zip"


def _plugin_summary(data: dict[str, Any]) -> PluginSummaryResource:
    capabilities, movie_number = PluginTestService.describe(data["plugin_id"])
    return PluginSummaryResource(
        **data,
        test_capabilities=list(capabilities),
        test_movie_number=movie_number,
    )


def _plugin_detail(data: dict[str, Any]) -> PluginDetailResource:
    capabilities, movie_number = PluginTestService.describe(data["plugin_id"])
    return PluginDetailResource(
        **data,
        test_capabilities=list(capabilities),
        test_movie_number=movie_number,
    )


@router.get("", response_model=list[PluginSummaryResource])
def list_plugins():
    return [_plugin_summary(item) for item in PluginManager().list_plugins()]


def _binding_resource() -> PluginBindingResource:
    data = PluginBindingService.describe()
    return PluginBindingResource(
        stats_source=data["stats_source"],
        review_source=data["review_source"],
        stats_source_available=data["stats_source_available"],
        review_source_available=data["review_source_available"],
        stats_source_display_name=(
            resolve_text(
                data["stats_source_display_name"] or "",
                data["stats_source_display_name_i18n"],
            )
            or None
        ),
        review_source_display_name=(
            resolve_text(
                data["review_source_display_name"] or "",
                data["review_source_display_name_i18n"],
            )
            or None
        ),
        stats_fields=[
            StatsFieldResource(
                key=field.key,
                label=resolve_text(field.key, field.label_i18n),
                value_type=field.value_type,
                min=field.min,
                max=field.max,
                unit=field.unit,
            )
            for field in data["stats_fields"]
        ],
        heat_version=data["heat_version"],
        available_stats_sources=[
            PluginBindingSourceResource(
                plugin_id=item["plugin_id"],
                display_name=resolve_text(
                    item["display_name"], item["display_name_i18n"]
                ),
            )
            for item in data["available_stats_sources"]
        ],
        available_review_sources=[
            PluginBindingSourceResource(
                plugin_id=item["plugin_id"],
                display_name=resolve_text(
                    item["display_name"], item["display_name_i18n"]
                ),
            )
            for item in data["available_review_sources"]
        ],
    )


@router.get("/bindings", response_model=PluginBindingResource)
def get_plugin_bindings():
    return _binding_resource()


@router.put("/bindings", response_model=PluginBindingUpdateResource)
def update_plugin_bindings(payload: PluginBindingUpdateRequest):
    try:
        PluginBindingService.update(
            stats_source=payload.stats_source,
            review_source=payload.review_source,
        )
    except ValueError as exc:
        raise ApiError(422, "invalid_plugin_binding", str(exc)) from exc
    resource = _binding_resource()
    return PluginBindingUpdateResource(
        **resource.model_dump(),
        pending_restart=["api", "aps"],
    )


@router.get("/{plugin_id}", response_model=PluginDetailResource)
def get_plugin(plugin_id: str):
    detail = PluginManager().get_plugin(plugin_id)
    if detail is None:
        raise ApiError(404, "plugin_not_found", f"未知插件 plugin_id={plugin_id}")
    return _plugin_detail(detail)


@router.post("/{plugin_id}/test", response_model=PluginTestResultResource)
def test_plugin(plugin_id: str, payload: PluginTestRequest):
    manager = PluginManager()
    detail = manager.get_plugin(plugin_id)
    if detail is None:
        raise ApiError(404, "plugin_not_found", f"未知插件 plugin_id={plugin_id}")
    if not detail["enabled"]:
        raise ApiError(
            409,
            "plugin_test_disabled",
            translate("errors.plugin_test_disabled"),
        )
    if detail["load_status"] != "ok":
        raise ApiError(
            409,
            "plugin_test_unavailable",
            translate("errors.plugin_test_unavailable"),
        )
    try:
        return PluginTestService.run(plugin_id, payload.capability)
    except PluginTestCapabilityUnsupported as exc:
        raise ApiError(
            422,
            "plugin_test_unsupported",
            translate("errors.plugin_test_unsupported"),
        ) from exc
    except PluginTestUnavailable as exc:
        raise ApiError(
            409,
            "plugin_test_unavailable",
            translate("errors.plugin_test_unavailable"),
        ) from exc


@router.get("/{plugin_id}/settings", response_model=PluginSettingsResource, response_model_exclude_none=True)
def get_plugin_settings(plugin_id: str):
    manager = PluginManager()
    try:
        settings_values = manager.get_plugin_settings(plugin_id)
        definition = manager.get_plugin_settings_definition(plugin_id)
    except PluginSettingsValidationError as exc:
        raise ApiError(422, "invalid_plugin_settings", str(exc), {"fields": exc.errors}) from exc
    except ValueError as exc:
        raise ApiError(404, "plugin_not_found", str(exc)) from exc
    return PluginSettingsResource(settings=settings_values, **definition)


@router.put("/{plugin_id}/settings", response_model=PluginSettingsUpdateResource, response_model_exclude_none=True)
def update_plugin_settings(plugin_id: str, payload: dict[str, Any] = Body(...)):
    manager = PluginManager()
    try:
        settings_values = manager.set_plugin_settings(plugin_id, payload)
    except PluginSettingsValidationError as exc:
        raise ApiError(422, "invalid_plugin_settings", str(exc), {"fields": exc.errors}) from exc
    except ValueError as exc:
        raise ApiError(404, "plugin_not_found", str(exc)) from exc
    return PluginSettingsUpdateResource(
        settings=settings_values,
        pending_restart=["api", "aps"],
    )


@router.post("", response_model=PluginInstallResponse, status_code=201)
def install_plugin(
    request: Request,
    file: UploadFile = File(...),
    sha256: str | None = Form(default=None),
    enable: bool = Form(default=True),
):
    _check_upload_size(request)
    manager = PluginManager()
    temp_path = _upload_temp_path(manager)
    try:
        with temp_path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        result = manager.install_zip(temp_path, sha256=sha256, enable=enable)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(422, "plugin_install_failed", f"插件安装失败: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)
    return PluginInstallResponse(
        plugin_id=result["plugin_id"],
        version=result["version"],
        pending_restart=manager.pending_restart_for(result["plugin_id"]),
    )


@router.post("/{plugin_id}/upgrade", response_model=PluginInstallResponse)
def upgrade_plugin(
    plugin_id: str,
    request: Request,
    file: UploadFile = File(...),
    sha256: str | None = Form(default=None),
):
    """升级已安装插件；发布后由用户自行重启容器使新代码生效。"""
    _check_upload_size(request)
    manager = PluginManager()
    if manager.get_plugin(plugin_id) is None:
        raise ApiError(404, "plugin_not_found", f"未知插件 plugin_id={plugin_id}")
    temp_path = _upload_temp_path(manager)
    try:
        with temp_path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        result = manager.upgrade_zip(plugin_id, temp_path, sha256=sha256)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(422, "plugin_upgrade_failed", f"插件升级失败: {exc}") from exc
    finally:
        temp_path.unlink(missing_ok=True)
    return PluginInstallResponse(
        plugin_id=result["plugin_id"],
        version=result["version"],
        pending_restart=manager.pending_restart_for(result["plugin_id"]),
    )


@router.patch("/{plugin_id}", response_model=PluginSummaryResource)
def set_plugin_enabled(plugin_id: str, enabled: bool):
    manager = PluginManager()
    try:
        manager.set_enabled(plugin_id, enabled)
    except ValueError as exc:
        raise ApiError(404, "plugin_not_found", str(exc)) from exc
    detail = manager.get_plugin(plugin_id)
    return _plugin_summary(detail)


@router.delete("/{plugin_id}", response_model=PluginInstallResponse)
def remove_plugin(plugin_id: str):
    manager = PluginManager()
    detail = manager.get_plugin(plugin_id)
    if detail is None:
        raise ApiError(404, "plugin_not_found", f"未知插件 plugin_id={plugin_id}")
    try:
        PluginRemovalService.remove(plugin_id)
    except PluginInUseError as exc:
        raise ApiError(409, "plugin_in_use", str(exc), exc.details) from exc
    except PluginBindingInUseError as exc:
        raise ApiError(409, "plugin_in_use", str(exc), exc.details) from exc
    except ValueError as exc:
        raise ApiError(404, "plugin_not_found", str(exc)) from exc
    return PluginInstallResponse(
        plugin_id=plugin_id,
        version=detail["version"],
        pending_restart=["api", "aps"],
    )
