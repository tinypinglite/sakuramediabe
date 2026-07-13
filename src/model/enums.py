from enum import Enum


class RefreshTokenStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MediaLibraryBackend(str, Enum):
    LOCAL = "local"
    CLOUD115 = "cloud115"
