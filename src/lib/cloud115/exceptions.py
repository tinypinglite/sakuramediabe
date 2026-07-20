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


class Cloud115VideoNotReadyError(Cloud115Error):
    """视频转码未就绪（响应 ``file_status != 1``）。

    文件真实存在且登录态有效，只是 115 尚未产出可播放的 HLS 流。调用方可以
    根据 ``file_status`` 安排重试，不应把媒体标记为永久失效。
    """

    def __init__(
        self,
        message: str,
        *,
        file_status: int | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.file_status = file_status
        super().__init__(message, endpoint=endpoint)


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


class Cloud115RiskControlError(Cloud115Error):
    """触发 115 风控：webapi 前置的阿里云 WAF 直接返回裸 HTTP 405（非 115 应用层 errno）。

    与 RateLimitedError（429，退避几秒可恢复）严格区分：这是账号/cookie 被标记异常并
    冻结一段时间（分钟到小时级），继续发请求只会不断制造新的 405、加深封禁、延长冻结。
    上层应立即停止对该账号的后续请求（熔断），待冷却后再重试。
    """

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        self.method = method
        self.url = url
        super().__init__(message, endpoint=url)


class Cloud115OfflineQuotaExceededError(Cloud115Error):
    """离线下载本月配额用尽。

    非 VIP 账号一般 5 次/月，VIP 200 次/月。errno 常见 10004 / 10008 等（"离线数已达上限"）。
    与 RateLimitedError 严格区分：不是限速、不是可以退避重试的；本月配额清 0 就是清 0。
    上层应引导用户：等下月 / 升级 VIP / 删除已完成任务腾出配额（配额不会因删除任务恢复，
    但已完成任务清理后 UI 观感更清爽）。
    """


class Cloud115OfflineTaskExistsError(Cloud115Error):
    """离线任务已存在：相同 info_hash / URL 已在离线列表里（重复提交）。

    与 Cloud115OfflineQuotaExceededError 严格区分——两者数字都可能是 10008，但字段不同：
      - 配额用尽：落在 **errno**（10004 / 10008），是硬失败。
      - 任务已存在：落在 **errcode**（10008，errtype="war"，error_msg="任务已存在"），是良性告警，
        不扣配额、旧任务仍在。
    上层通常可当幂等成功处理（复用旧任务 info_hash），或提示"该种子已在下载列表"；
    若要改文件选择，必须先 delete_offline_tasks 删旧任务再重加。附 info_hash 便于定位旧任务。
    """

    def __init__(
        self,
        message: str,
        *,
        info_hash: str | None = None,
        errno: int | None = None,
        endpoint: str | None = None,
    ):
        self.info_hash = info_hash
        super().__init__(message, errno=errno, endpoint=endpoint)
