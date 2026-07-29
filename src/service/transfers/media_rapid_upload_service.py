"""兼容旧 import path；批量秒传实现已迁至 ``transfers.media_rapid_upload``。

只重导对外安全的两个名字：``MediaRapidUploadService`` 是多继承组合出的门面，
``MediaRapidUploadQueryService`` 自身闭包。command/executor/item_executor/
recovery/state_machine 五个 base 内部互相 ``cls.`` 引用兄弟类的方法，脱离门面
单独调用即 AttributeError，故不再从这里暴露。
"""

from src.service.transfers.media_rapid_upload.facade import MediaRapidUploadService
from src.service.transfers.media_rapid_upload.query_service import (
    MediaRapidUploadQueryService,
)

__all__ = [
    "MediaRapidUploadQueryService",
    "MediaRapidUploadService",
]
