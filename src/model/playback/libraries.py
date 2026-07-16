import peewee

from src.model.base import BaseModel, JsonTextField
from src.model.enums import MediaLibraryBackend
from src.model.mixins import TimestampedMixin


class MediaLibrary(TimestampedMixin, BaseModel):
    name = peewee.CharField(max_length=255, unique=True, index=True)
    backend = peewee.CharField(max_length=32, default=MediaLibraryBackend.LOCAL.value)
    backend_config = JsonTextField(default=dict)
    # 后端账号的内部唯一标识。cloud115 使用 ``cloud115:<user_id>``；本地库为 NULL。
    # 不放进 backend_config，避免凭据脱敏或配置变更破坏唯一性约束。
    backend_account_key = peewee.CharField(max_length=255, null=True, unique=True)

    class Meta:
        table_name = "media_library"
