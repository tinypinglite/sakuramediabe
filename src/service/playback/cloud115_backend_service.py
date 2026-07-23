"""兼容旧 import path；Cloud115 共享集成已迁至 ``src.service.cloud115``。"""

from src.service.cloud115 import (
    CLOUD115_DOWNLOADS_ROOT_NAME,
    CLOUD115_LIBRARY_ROOT_NAME,
    Cloud115KeepaliveService,
    assert_cid_outside_library_root,
    cloud115_client_for,
    ensure_download_root_cid,
    find_or_create_subdir,
    map_cloud115_error,
    require_cloud115_library,
)

__all__ = [
    "CLOUD115_DOWNLOADS_ROOT_NAME",
    "CLOUD115_LIBRARY_ROOT_NAME",
    "Cloud115KeepaliveService",
    "assert_cid_outside_library_root",
    "cloud115_client_for",
    "ensure_download_root_cid",
    "find_or_create_subdir",
    "map_cloud115_error",
    "require_cloud115_library",
]
