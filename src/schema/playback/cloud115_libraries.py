from typing import Literal

from src.schema.common.base import SchemaModel

# 扫码状态字符串——直接映射 SDK 的 QrStatus，小写方便前端消费。
QrStatusLiteral = Literal["waiting", "scanned", "confirmed", "expired", "canceled"]


class Cloud115QrTokenResource(SchemaModel):
    """扫码会话初始化返回。qrcode_png_base64 前端拼 data URL 直接展示。"""

    uid: str
    time: int
    sign: str
    qrcode_png_base64: str


class Cloud115QrStatusRequest(SchemaModel):
    """长轮询状态请求。原样透传 SDK 需要的 3 个字段。"""

    uid: str
    time: int
    sign: str


class Cloud115QrStatusResource(SchemaModel):
    status: QrStatusLiteral


class Cloud115LibraryCreateRequest(SchemaModel):
    """扫码 CONFIRMED 后，用 uid 换 cookies 并落库为 cloud115 媒体库。

    app 决定 cookies 落在哪个 115 登录槽（默认 alipaymini 不互踢），可用值见
    src.lib.cloud115.qrlogin.APPS。
    """

    name: str
    uid: str
    app: str = "alipaymini"


class Cloud115LibraryReauthRequest(SchemaModel):
    """为已绑定的 cloud115 媒体库换取一组新的 cookies。"""

    uid: str
    app: str = "alipaymini"


class Cloud115DirEntryResource(SchemaModel):
    """115 目录浏览单条目（前端目录选择器消费的最小字段集）。"""

    entry_id: str
    name: str
    is_dir: bool
    size: int
    is_video: bool
    mtime: int


class Cloud115BrowseResponse(SchemaModel):
    """115 目录浏览一页。

    root_cid 是该库的管理目录（sakuramedia/）cid——前端目录选择器据此禁止用户
    把管理目录（或其子目录）选为导入源；服务端触发导入时还会再校验一次。
    """

    cid: str
    total: int
    offset: int
    limit: int
    root_cid: str
    entries: list[Cloud115DirEntryResource]
