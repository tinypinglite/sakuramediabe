"""Cloud115 业务集成边界。

本包负责媒体库配置、SDK client 生命周期和业务错误映射；底层 HTTP 协议仍由
``src.lib.cloud115`` 独立承担。
"""

from .library_client import (
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
