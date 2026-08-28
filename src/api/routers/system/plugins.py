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
from src.plugins.installer import MAX_ARCHIVE_BYTES
from src.plugins.manager import PluginManager, PluginSettingsValidationError
from src.schema.system.plugins import (
    PluginDetailResource,
    PluginInstallResponse,
    PluginSettingsResource,
    PluginSettingsUpdateResource,
    PluginSummaryResource,
)
from src.service.system.plugin_removal_service import (
    PluginInUseError,
    PluginRemovalService,
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


@router.get("", response_model=list[PluginSummaryResource])
def list_plugins():
    return PluginManager().list_plugins()


@router.get("/{plugin_id}", response_model=PluginDetailResource)
def get_plugin(plugin_id: str):
    detail = PluginManager().get_plugin(plugin_id)
    if detail is None:
        raise ApiError(404, "plugin_not_found", f"未知插件 plugin_id={plugin_id}")
    return detail


@router.get("/{plugin_id}/settings", response_model=PluginSettingsResource)
def get_plugin_settings(plugin_id: str):
    manager = PluginManager()
    try:
        settings_values = manager.get_plugin_settings(plugin_id)
    except ValueError as exc:
        raise ApiError(404, "plugin_not_found", str(exc)) from exc
    return PluginSettingsResource(settings=settings_values)


@router.put("/{plugin_id}/settings", response_model=PluginSettingsUpdateResource)
def update_plugin_settings(plugin_id: str, payload: dict[str, Any] = Body(...)):
    manager = PluginManager()
    try:
        settings_values = manager.set_plugin_settings(plugin_id, payload)
    except PluginSettingsValidationError as exc:
        raise ApiError(422, "invalid_plugin_settings", str(exc)) from exc
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
    except Exception as exc:
        raise ApiError(422, "plugin_install_failed", f"插件安装失败: {exc}") from exc
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
    return PluginSummaryResource(
        plugin_id=detail["plugin_id"],
        display_name=detail["display_name"],
        version=detail["version"],
        host_api_version=detail["host_api_version"],
        enabled=detail["enabled"],
        load_status=detail["load_status"],
        load_error=detail["load_error"],
    )


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
    except ValueError as exc:
        raise ApiError(404, "plugin_not_found", str(exc)) from exc
    return PluginInstallResponse(
        plugin_id=plugin_id,
        version=detail["version"],
        pending_restart=["api", "aps"],
    )
