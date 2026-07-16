"""115 网盘极简异步客户端。

覆盖播放/查找/缩略图/离线下载及本地文件秒传 5 类上层需求需要的 HTTP 接口 + cookies 认证；
另含扫码登录（Cloud115QrLogin，独立于 cookies，用于首次获取 cookies）。
不含普通文件上传、分享、事件订阅等；`rapid_upload` 仅执行秒传初始化，不会回退上传。

上层使用方式：
    from src.lib.cloud115 import Cloud115Client, DirectUrl

    async with Cloud115Client(cookies=os.environ["COOKIE_115"]) as client:
        entries, total = await client.list_dir("0", limit=50)
        du = await client.get_download_url(pickcode, user_agent="Mozilla/5.0 ...")

Cookies 保活：客户端内部维护 cookies dict，每次响应自动 merge 服务端 Set-Cookie
（主要是 30 分钟过期的 acw_tc 阿里云 WAF token）。上层可定时 `snapshot_cookies()`
落盘，进程重启也不必每次首启就撞 acw_tc 过期。
"""

from src.lib.cloud115.client import Cloud115Client
from src.lib.cloud115.qrlogin import APPS, Cloud115QrLogin
from src.lib.cloud115.reader import Cloud115RangeReader
from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
    Cloud115CipherError,
    Cloud115Error,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115OfflineQuotaExceededError,
    Cloud115OfflineTaskExistsError,
    Cloud115RateLimitedError,
    Cloud115RequestError,
)
from src.lib.cloud115.types import (
    Cloud115CookieStatus,
    DirBreadcrumb,
    DirectUrl,
    DirEntry,
    DirMeta,
    FileMeta,
    OfflineQuota,
    OfflineTask,
    OfflineTaskAddResult,
    OfflineTaskPage,
    RapidUploadResult,
    RapidUploadStatus,
    QrCodeToken,
    QrLoginResult,
    QrStatus,
)

__all__ = [
    "Cloud115Client",
    "Cloud115QrLogin",
    "Cloud115RangeReader",
    "Cloud115CookieStatus",
    "APPS",
    "QrCodeToken",
    "QrStatus",
    "QrLoginResult",
    "DirEntry",
    "DirMeta",
    "DirBreadcrumb",
    "FileMeta",
    "DirectUrl",
    "OfflineTask",
    "OfflineTaskPage",
    "OfflineQuota",
    "OfflineTaskAddResult",
    "RapidUploadResult",
    "RapidUploadStatus",
    "Cloud115Error",
    "Cloud115AuthError",
    "Cloud115NotFoundError",
    "Cloud115RequestError",
    "Cloud115CipherError",
    "Cloud115RateLimitedError",
    "Cloud115MembershipRequiredError",
    "Cloud115OfflineQuotaExceededError",
    "Cloud115OfflineTaskExistsError",
]
