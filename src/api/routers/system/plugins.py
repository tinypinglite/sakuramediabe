"""插件管理 API：安装、升级、回滚、启停、依赖重装、卸载与状态查询。"""

from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.plugins.installer import MAX_ARCHIVE_BYTES
from src.plugins.manager import PluginManager
from src.schema.system.plugins import (
    PluginDetailResource,
    PluginInstallResponse,
    PluginSummaryResource,
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


def _upload_temp_path(manager: PluginManager, suffix: str = ".zip") -> Path:
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
    return upload_dir / f"{uuid.uuid4().hex}{suffix}"


@router.get("", response_model=list[PluginSummaryResource])
def list_plugins():
    return PluginManager().list_plugins()


@router.get("/{plugin_id}", response_model=PluginDetailResource)
def get_plugin(plugin_id: str):
    detail = PluginManager().get_plugin(plugin_id)
    if detail is None:
        raise ApiError(404, "plugin_not_found", f"未知插件 plugin_id={plugin_id}")
    return detail


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
        result = manager.install(temp_path, sha256=sha256, enable=enable)
    except Exception as exc:
        raise ApiError(
            422,
            "plugin_install_failed",
            f"插件安装失败: {exc}",
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)
    return PluginInstallResponse(
        plugin_id=result.plugin_id,
        version=result.version,
        pending_restart=list(result.pending_restart),
    )


@router.post("/{plugin_id}/update", response_model=PluginInstallResponse)
def update_plugin(
    request: Request,
    plugin_id: str,
    file: UploadFile = File(...),
    sha256: str | None = Form(default=None),
):
    _check_upload_size(request)
    manager = PluginManager()
    temp_path = _upload_temp_path(manager)
    try:
        with temp_path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        result = manager.update(plugin_id, temp_path, sha256=sha256)
    except Exception as exc:
        raise ApiError(
            422,
            "plugin_update_failed",
            f"插件升级失败: {exc}",
        ) from exc
    finally:
        temp_path.unlink(missing_ok=True)
    return PluginInstallResponse(
        plugin_id=result.plugin_id,
        version=result.version,
        pending_restart=list(result.pending_restart),
    )


@router.post("/{plugin_id}/rollback", response_model=PluginInstallResponse)
def rollback_plugin(plugin_id: str):
    manager = PluginManager()
    try:
        result = manager.rollback(plugin_id)
    except Exception as exc:
        raise ApiError(422, "plugin_rollback_failed", f"插件回滚失败: {exc}") from exc
    return PluginInstallResponse(
        plugin_id=result.plugin_id,
        version=result.version,
        pending_restart=list(result.pending_restart),
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
        deps_status=detail["deps_status"],
        load_status=detail["load_status"],
        load_error=detail["load_error"],
        installed_at=detail["installed_at"],
    )


@router.post("/{plugin_id}/deps/install", response_model=PluginInstallResponse)
def reinstall_plugin_deps(plugin_id: str):
    manager = PluginManager()
    try:
        manager.reinstall_deps(plugin_id)
    except Exception as exc:
        raise ApiError(
            422,
            "plugin_deps_install_failed",
            f"插件依赖重装失败: {exc}",
        ) from exc
    return PluginInstallResponse(
        plugin_id=plugin_id,
        version="",
        pending_restart=["api", "aps"],
    )


@router.delete("/{plugin_id}", response_model=PluginInstallResponse)
def uninstall_plugin(plugin_id: str, purge_data: bool = False):
    manager = PluginManager()
    try:
        result = manager.uninstall(plugin_id, purge_data=purge_data)
    except Exception as exc:
        raise ApiError(
            422,
            "plugin_uninstall_failed",
            f"插件卸载失败: {exc}",
        ) from exc
    return PluginInstallResponse(
        plugin_id=result.plugin_id,
        version=result.version,
        pending_restart=list(result.pending_restart),
    )
