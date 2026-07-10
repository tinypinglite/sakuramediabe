"""115 SDK 异常层次。

风格对齐 src/metadata/_providers/exceptions.py：结构化字段暴露 + f-string message。
所有子类继承 Cloud115Error，上层用 except Cloud115Error 兜底即可。
"""

from __future__ import annotations


class Cloud115Error(Exception):
    """115 SDK 基础异常。"""

    def __init__(self, message: str, *, errno: int | None = None, endpoint: str | None = None):
        self.errno = errno
        self.endpoint = endpoint
        super().__init__(message)


class Cloud115AuthError(Cloud115Error):
    """cookie 过期 / 未登录 / UID 缺失或格式错误。

    通常由业务 errno（990009 / 990017 / 20130827 等）或 HTTP 401/403 触发。
    构造 Client 时若 UID 缺失也抛此异常，fail fast 避免延迟报错。
    """


class Cloud115NotFoundError(Cloud115Error):
    """文件/目录不存在，或 pickcode 无效，或资源被封禁（4100003 / 4100008 等）。

    包含"被 115 屏蔽"这种业务上等同于"拿不到资源"的情形。
    """


class Cloud115MembershipRequiredError(Cloud115Error):
    """接口需要 VIP 会员（errno=406 "需要VIP会员"）。

    与 AuthError 严格区分：cookies 有效、账号正常，但当前操作被 115 官方策略
    限定为 VIP 专属（视频在线播放、m3u8 转码、投屏等）。上层应引导用户升级会员，
    而不是让用户去重新填 cookies。
    """


class Cloud115RequestError(Cloud115Error):
    """HTTP 层错误、超时、5xx 重试耗尽后的最终失败。

    构造函数保留 method + url + detail，方便日志和排障。
    """

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        url: str | None = None,
        detail: str | None = None,
        errno: int | None = None,
    ):
        self.method = method
        self.url = url
        self.detail = detail
        super().__init__(message, errno=errno, endpoint=url)


class Cloud115CipherError(Cloud115Error):
    """RSA 加解密失败：响应密文长度非 128 倍数、PKCS#1 分隔符缺失等。"""


class Cloud115RateLimitedError(Cloud115Error):
    """429 或 errno 提示限流。

    附 retry_after_seconds（None 表示服务端未给 Retry-After header）。
    SDK 不做无脑重试，让上层 APS 决定退避窗口。
    """

    def __init__(self, message: str, *, retry_after_seconds: int | None = None):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)
