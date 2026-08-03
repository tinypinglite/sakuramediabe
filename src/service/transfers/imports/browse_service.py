"""后端文件系统目录浏览 service。

供可视化导入选择导入源目录使用：仅在配置的白名单根目录范围内浏览，
每次只列出一层的子目录与视频文件，落在白名单外或无权限的条目跳过。
"""

import os
from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.fs_browse import (
    assert_within_allowed_roots,
    is_video_file,
    is_within_allowed_roots,
    normalize_abs_path,
    resolve_allowed_roots,
)
from src.config.config import settings
from src.schema.transfers.media_import import (
    FilesystemEntryResource,
    FilesystemListResponse,
)


class FilesystemBrowseService:
    @classmethod
    def list_entries(cls, path: str | None = None) -> FilesystemListResponse:
        """列出指定目录下一层的子目录与视频文件，限定在白名单根目录范围内。"""
        roots = settings.media_import.browse_roots
        resolved_roots = resolve_allowed_roots(roots)
        if not resolved_roots:
            raise ApiError(403, "browse_not_configured", "未配置可浏览的根目录")

        # 缺省浏览：单根直接进入该根；多根则先列出各根供前端选择下钻。
        if not (path or "").strip():
            if len(resolved_roots) == 1:
                target = resolved_roots[0]
            else:
                return cls._build_roots_overview(resolved_roots)
        else:
            target = normalize_abs_path(path)
            assert_within_allowed_roots(target, roots)

        if not target.is_dir():
            raise ApiError(400, "path_not_directory", "目标路径不是目录", {"path": str(target)})

        entries: list[FilesystemEntryResource] = []
        try:
            scandir_iterator = os.scandir(target)
        except PermissionError as exc:
            raise ApiError(403, "path_forbidden", "无权访问该目录", {"path": str(target)}) from exc

        with scandir_iterator:
            for dir_entry in scandir_iterator:
                entry = cls._build_entry(dir_entry, roots)
                if entry is not None:
                    entries.append(entry)

        # 目录在前、视频在后，再各自按名称排序，保证前端展示稳定。
        entries.sort(key=lambda item: (item.type != "dir", item.name.lower()))
        parent = cls._resolve_parent(target, roots)
        return FilesystemListResponse(path=str(target), parent=parent, entries=entries)

    @staticmethod
    def _build_roots_overview(resolved_roots: list[Path]) -> FilesystemListResponse:
        """多白名单根时返回各根目录概览，path 为空表示这是根选择层。"""
        entries = [
            FilesystemEntryResource(
                name=root.name or str(root),
                path=str(root),
                type="dir",
                size=0,
                is_video=False,
            )
            for root in resolved_roots
            if root.is_dir()
        ]
        entries.sort(key=lambda item: item.name.lower())
        return FilesystemListResponse(path="", parent=None, entries=entries)

    @staticmethod
    def _resolve_parent(target: Path, roots) -> str | None:
        """仅当父目录仍落在白名单范围内时才暴露，避免越过根目录向上浏览。"""
        parent = target.parent
        if parent == target:
            return None
        if not is_within_allowed_roots(parent, roots):
            return None
        return str(parent)

    @staticmethod
    def _build_entry(dir_entry: os.DirEntry, roots) -> FilesystemEntryResource | None:
        entry_path = Path(dir_entry.path)
        # 解析后落在白名单外的条目（含指向白名单外的符号链接）直接跳过，不暴露其存在。
        if not is_within_allowed_roots(entry_path, roots):
            return None

        try:
            is_dir = dir_entry.is_dir(follow_symlinks=False)
            is_file = dir_entry.is_file(follow_symlinks=False)
        except OSError as exc:
            logger.debug("Filesystem browse skip entry path={} detail={}", dir_entry.path, exc)
            return None

        if is_dir:
            return FilesystemEntryResource(
                name=dir_entry.name,
                path=str(entry_path),
                type="dir",
                size=0,
                is_video=False,
            )

        if not is_file:
            return None

        video = is_video_file(entry_path)
        # 非视频文件不参与导入，不返回。
        if not video:
            return None

        try:
            size = dir_entry.stat(follow_symlinks=False).st_size
        except OSError:
            size = 0
        return FilesystemEntryResource(
            name=dir_entry.name,
            path=str(entry_path),
            type="video",
            size=size,
            is_video=True,
        )
