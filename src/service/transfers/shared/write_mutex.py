"""跨导入链路复用的远端写入互斥键。"""

from __future__ import annotations

from src.model import MediaLibrary
from src.model.enums import MediaLibraryBackend

LOCAL_IMPORT_MUTEX_KEY = "library_import:local"
CLOUD115_WRITE_MUTEX_KEY = "cloud115_write:global"


def cloud115_write_mutex_key(library: MediaLibrary) -> str:
    """所有 115 写入任务统一串行，避免远端目录/文件写入互相踩踏。"""
    if library.backend != MediaLibraryBackend.CLOUD115.value:
        raise ValueError("cloud115_write_mutex_requires_cloud115_library")
    return CLOUD115_WRITE_MUTEX_KEY


def library_import_mutex_key(*, backend: str, library: MediaLibrary) -> str:
    """本地导入独立串行，所有 115 导入共享全局写入锁。"""
    if backend == MediaLibraryBackend.LOCAL.value:
        return LOCAL_IMPORT_MUTEX_KEY
    return cloud115_write_mutex_key(library)
