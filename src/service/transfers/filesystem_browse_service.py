"""后端文件系统目录浏览 service。

供可视化导入选择导入源目录使用：仅列出一层的子目录与视频文件，命中黑名单或无权限的条目跳过。
"""

import os
from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.fs_browse import (
    assert_not_blacklisted,
    is_video_file,
    normalize_abs_path,
)
from src.config.config import settings
from src.schema.transfers.media_import import (
    FilesystemEntryResource,
    FilesystemListResponse,
)


class FilesystemBrowseService:
    @classmethod
    def list_entries(cls, path: str | None = None) -> FilesystemListResponse:
        """列出指定目录下一层的子目录与视频文件。"""
        blacklist = settings.media_import.browse_blacklist
        # 缺省从根目录开始浏览，方便前端逐层下钻。
        target = normalize_abs_path(path) if (path or "").strip() else Path("/")
        assert_not_blacklisted(target, blacklist)

        if not target.is_dir():
            raise ApiError(400, "path_not_directory", "目标路径不是目录", {"path": str(target)})

        entries: list[FilesystemEntryResource] = []
        try:
            scandir_iterator = os.scandir(target)
        except PermissionError as exc:
            raise ApiError(403, "path_forbidden", "无权访问该目录", {"path": str(target)}) from exc

        with scandir_iterator:
            for dir_entry in scandir_iterator:
                entry = cls._build_entry(dir_entry, blacklist)
                if entry is not None:
                    entries.append(entry)

        # 目录在前、视频在后，再各自按名称排序，保证前端展示稳定。
        entries.sort(key=lambda item: (item.type != "dir", item.name.lower()))
        parent = str(target.parent) if target.parent != target else None
        return FilesystemListResponse(path=str(target), parent=parent, entries=entries)

    @staticmethod
    def _build_entry(dir_entry: os.DirEntry, blacklist) -> FilesystemEntryResource | None:
        entry_path = Path(dir_entry.path)
        try:
            assert_not_blacklisted(entry_path, blacklist)
        except ApiError:
            # 黑名单子项直接跳过，不暴露其存在。
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
        # 非视频文件不参与导入，但仍返回供前端展示目录内容全貌。
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
