from enum import Enum


class RefreshTokenStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MediaLibraryBackend(str, Enum):
    LOCAL = "local"
    CLOUD115 = "cloud115"


class DownloadClientKind(str, Enum):
    # 下载入口种类：qbittorrent 是独立部署的本地下载器；cloud115 是挂在 cloud115 媒体库上的
    # 离线下载能力（无独立部署，凭据走 media_library.backend_config）。
    QBITTORRENT = "qbittorrent"
    CLOUD115 = "cloud115"
