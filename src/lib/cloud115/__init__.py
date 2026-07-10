"""115 网盘极简异步客户端。

只覆盖播放/查找/缩略图 3 类上层需求需要的 5 个 HTTP 接口 + cookies 认证。
不含二维码登录、离线下载、上传、分享、事件订阅等。

上层使用方式：
    from src.lib.cloud115 import Cloud115Client, DirectUrl

    async with Cloud115Client(cookies=os.environ["COOKIE_115"]) as client:
        entries, total = await client.list_dir("0", limit=50)
        du = await client.get_download_url(pickcode, user_agent="Mozilla/5.0 ...")
"""

from src.lib.cloud115.client import Cloud115Client
from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
    Cloud115CipherError,
    Cloud115Error,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115RateLimitedError,
    Cloud115RequestError,
)
from src.lib.cloud115.types import (
    DirectUrl,
    DirEntry,
    FileMeta,
    VideoDefinition,
    VideoInfo,
    VideoSegment,
)

__all__ = [
    "Cloud115Client",
    "DirEntry",
    "FileMeta",
    "DirectUrl",
    "VideoInfo",
    "VideoDefinition",
    "VideoSegment",
    "Cloud115Error",
    "Cloud115AuthError",
    "Cloud115NotFoundError",
    "Cloud115RequestError",
    "Cloud115CipherError",
    "Cloud115RateLimitedError",
    "Cloud115MembershipRequiredError",
]
