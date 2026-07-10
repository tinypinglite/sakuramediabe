"""115 网盘极简异步客户端。

覆盖播放/查找/缩略图/离线下载（含 BT 选文件）4 类上层需求需要的 HTTP 接口 + cookies 认证。
不含二维码登录、通用文件上传（仅 .torrent 的 sample 上传）、分享、事件订阅等。

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
    Cloud115VideoNotReadyError,
)
from src.lib.cloud115.types import (
    DirBreadcrumb,
    DirectUrl,
    DirEntry,
    DirMeta,
    FileMeta,
    OfflineQuota,
    OfflineTask,
    OfflineTaskAddResult,
    OfflineTaskPage,
    TorrentFileEntry,
    TorrentInfo,
    VideoDefinition,
    VideoInfo,
    VideoSegment,
)

__all__ = [
    "Cloud115Client",
    "DirEntry",
    "DirMeta",
    "DirBreadcrumb",
    "FileMeta",
    "DirectUrl",
    "VideoInfo",
    "VideoDefinition",
    "VideoSegment",
    "OfflineTask",
    "OfflineTaskPage",
    "OfflineQuota",
    "OfflineTaskAddResult",
    "TorrentFileEntry",
    "TorrentInfo",
    "Cloud115Error",
    "Cloud115AuthError",
    "Cloud115NotFoundError",
    "Cloud115RequestError",
    "Cloud115CipherError",
    "Cloud115RateLimitedError",
    "Cloud115MembershipRequiredError",
    "Cloud115VideoNotReadyError",
    "Cloud115OfflineQuotaExceededError",
    "Cloud115OfflineTaskExistsError",
]
