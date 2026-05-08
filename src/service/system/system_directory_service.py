from __future__ import annotations

from pathlib import Path

from src.api.exception.errors import ApiError
from src.schema.system.activity import DirectoryEntryResource, DirectoryListResource


class SystemDirectoryService:
    """安全浏览容器内目录，供前端选择后端可访问的导入源。"""

    BLOCKED_ROOTS = {
        Path("/proc"),
        Path("/sys"),
        Path("/dev"),
        Path("/run"),
        Path("/tmp"),
        Path("/usr"),
    }

    @classmethod
    def resolve_safe_path(cls, raw_path: str) -> Path:
        value = (raw_path or "").strip()
        if not value.startswith("/"):
            raise ApiError(422, "invalid_directory_path", "Path must be absolute", {"path": raw_path})
        raw_parts = Path(value).parts
        if ".." in raw_parts:
            raise ApiError(422, "invalid_directory_path", "Path cannot contain '..'", {"path": raw_path})
        try:
            resolved = Path(value).expanduser().resolve()
        except OSError as exc:
            raise ApiError(422, "invalid_directory_path", "Path cannot be resolved", {"path": raw_path, "detail": str(exc)}) from exc
        if cls._is_blocked(resolved):
            raise ApiError(422, "blocked_directory_path", "Path is not allowed", {"path": str(resolved)})
        return resolved

    @classmethod
    def _blocked_roots(cls) -> set[Path]:
        roots = set(cls.BLOCKED_ROOTS)
        for blocked in cls.BLOCKED_ROOTS:
            try:
                roots.add(blocked.resolve())
            except OSError:
                roots.add(blocked)
        return roots

    @classmethod
    def _is_blocked(cls, path: Path) -> bool:
        return any(path == blocked or blocked in path.parents for blocked in cls._blocked_roots())

    @classmethod
    def _entry_resource(cls, path: Path) -> DirectoryEntryResource | None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if cls._is_blocked(resolved):
            return None
        is_symlink = path.is_symlink()
        readable = False
        selectable = False
        try:
            is_directory = path.is_dir()
            if is_directory:
                path.iterdir().__next__()
                readable = True
        except StopIteration:
            readable = True
        except (OSError, PermissionError):
            readable = False
        selectable = bool(readable and path.is_dir() and not cls._is_blocked(resolved))
        return DirectoryEntryResource(
            name=path.name or str(path),
            path=str(path),
            readable=readable,
            selectable=selectable,
            is_symlink=is_symlink,
        )

    @classmethod
    def list_directories(cls, raw_path: str) -> DirectoryListResource:
        directory = cls.resolve_safe_path(raw_path)
        if not directory.exists():
            raise ApiError(404, "directory_not_found", "Directory not found", {"path": str(directory)})
        if not directory.is_dir():
            raise ApiError(422, "path_not_directory", "Path is not a directory", {"path": str(directory)})
        entries: list[DirectoryEntryResource] = []
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except (OSError, PermissionError) as exc:
            raise ApiError(403, "directory_not_readable", "Directory is not readable", {"path": str(directory), "detail": str(exc)}) from exc
        for child in children:
            if not child.is_dir():
                continue
            entry = cls._entry_resource(child)
            if entry is not None:
                entries.append(entry)
        parent_path = None
        if directory != Path("/"):
            parent = directory.parent.resolve()
            if not cls._is_blocked(parent):
                parent_path = str(parent)
        return DirectoryListResource(path=str(directory), parent_path=parent_path, entries=entries)
