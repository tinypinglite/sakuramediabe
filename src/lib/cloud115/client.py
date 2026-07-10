"""115 网盘极简异步客户端。

覆盖播放/查找/缩略图/离线下载上层需求的 HTTP 接口：
    - check_cookies_alive
    - list_dir
    - file_info                 （by file_id）
    - pickcode_info             （by pickcode，业务侧持久化的稳定 ID 通常是 pickcode）
    - dir_info                  （目录元信息 + 面包屑）
    - get_download_url          （非会员亦可，但直链限速 100KB/s + CDN 多请求拉黑）
    - get_video_info            （VIP 专属：拿视频 master m3u8 + 清晰度列表）
    - get_video_segments        （VIP 专属：拿指定/最高清晰度的 HLS ts 分段列表）
    - list_offline_tasks / offline_quota / default_download_dir      （离线下载：读）
    - add_offline_urls / delete_offline_tasks / clear_offline_tasks  （离线下载：写）
    - restart_offline_task                                           （离线下载：失败重试）
    - upload_torrent / torrent_info / add_task_bt                    （离线 BT：传种子/解析文件列表/按选择建任务）
    - snapshot_cookies / update_cookies （cookies 保活/热替换，见 _merge_set_cookies）
    - 构造函数（cookies 字符串 -> httpx.AsyncClient）

不含：二维码登录（在独立的 qrlogin.Cloud115QrLogin，登录发生在拿到 cookies 之前）、
通用文件上传（仅 .torrent 的 sample 上传用于离线选文件）、分享、事件订阅、图片 CDN 等。
不依赖任何业务 model / service / schema，纯 HTTP + RSA 层。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Literal
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from loguru import logger

from src.lib.cloud115.cipher import decrypt_response, encrypt_payload
from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
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


# 归类到具体异常子类的 errno 集合。全部来自 p115client 观察 + 反向验证。
# 认证类：session 失效 / 冻结 / 需短信验证。
# errno=99 "请重新登录"：短时高频 downurl 触发的账号级冷却，实测遇到（2026-07-12）。
_AUTH_ERRNOS: frozenset[int] = frozenset({
    50003, 50004, 99, 911, 20130827, 99999, 990009, 990017,
})
# 未找到类：文件/目录不存在 / pickcode 无效 / 资源被封禁。
_NOT_FOUND_ERRNOS: frozenset[int] = frozenset({
    20121, 20125, 990002, 4100003, 4100008,
})
# 请求参数错误：调用方 bug，不是重试能解决的。
_REQUEST_ERRNOS: frozenset[int] = frozenset({990005})
# 需要 VIP 会员：视频在线播放、m3u8 转码等 VIP 专属接口的策略拒绝。
_MEMBERSHIP_REQUIRED_ERRNOS: frozenset[int] = frozenset({406})
# 离线下载月度配额用尽：账号本月离线次数已达上限。observed errno = 10008（未 VIP）/ 10004（少数场景）。
# 不与 RateLimited 混淆：这个不是限速、不能靠退避恢复。
_OFFLINE_QUOTA_EXCEEDED_ERRNOS: frozenset[int] = frozenset({10004, 10008})

# clear_offline_tasks 支持的 scope 及对应服务端 flag。
# 参见 clouddownload_task_clear 文档：
#   0=已完成 / 1=全部 / 2=已失败 / 3=进行中 / 4=已完成+删源 / 5=全部+删源
_CLEAR_SCOPE_TO_FLAG: dict[str, int] = {
    "finished": 0,
    "all": 1,
    "failed": 2,
    "running": 3,
    "finished_with_source": 4,
    "all_with_source": 5,
}

ClearScope = Literal[
    "finished", "all", "failed", "running",
    "finished_with_source", "all_with_source",
]


class Cloud115Client:
    """115 网盘异步客户端（cookies 认证）。

    线程/协程安全：httpx.AsyncClient 允许并发请求。
    使用方式：
        async with Cloud115Client(cookies="UID=...; CID=...; SEID=...; KID=...") as c:
            alive = await c.check_cookies_alive()
            entries, total = await c.list_dir("0", limit=50)
    """

    _BASE_MY = "https://my.115.com"
    _BASE_WEBAPI = "https://webapi.115.com"
    _BASE_PROAPI = "https://proapi.115.com"

    # 默认 UA：稳定的 Chrome UA，用于客户端 -> 115 的所有请求（downurl 拿链接的场景由调用方传 UA）。
    _DEFAULT_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    # list_dir limit 的服务端硬上限。
    _LIST_DIR_MAX_LIMIT = 1150

    # UID cookie 前段就是 user_id。格式：UID=<int>_A1_<unix_ts>
    _UID_PATTERN = re.compile(r"UID=(\d+)_")

    # 退避重试：最多 2 次重试（首次立即，第 2 次 sleep 0.5s，第 3 次 sleep 1.0s）
    _MAX_RETRIES = 2
    _RETRY_BACKOFF_STEP = 0.5

    def __init__(
        self,
        cookies: str,
        *,
        user_agent: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not cookies or "UID=" not in cookies:
            # cookies 缺失或不含 UID：SDK 层直接判死，不做延迟报错
            raise Cloud115AuthError("cookies missing or has no UID field")
        # cookies 存成 dict（保序）便于响应到达后 merge 服务端 Set-Cookie 更新
        self._cookies_dict: dict[str, str] = self._parse_cookies(cookies)
        self._user_id = self._parse_user_id_from_dict(self._cookies_dict)
        self._cookies_lock = asyncio.Lock()
        self._user_agent = user_agent or self._DEFAULT_UA
        # 外部注入的 client 由调用方负责关闭（不做 owned/borrowed 引用计数简化状态）
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,          # 忽略环境代理，避免流量意外走公司/系统代理
            follow_redirects=False,   # check_cookies_alive 靠 302 判死活；downurl 也不能自动跟 302
        )

    # ---- 构造与生命周期 ----

    @staticmethod
    def _parse_cookies(cookies: str) -> dict[str, str]:
        """把 'K1=V1; K2=V2' 解析成保序 dict。忽略无 '=' 的碎片。"""
        out: dict[str, str] = {}
        for part in cookies.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            key, _, value = part.partition("=")
            key = key.strip()
            if key:
                out[key] = value.strip()
        return out

    @classmethod
    def _parse_user_id_from_dict(cls, cookies_dict: dict[str, str]) -> str:
        uid = cookies_dict.get("UID", "")
        match = cls._UID_PATTERN.match(f"UID={uid}") if uid else None
        if not match:
            raise Cloud115AuthError("UID missing or malformed (expected 'UID=<int>_A1_<ts>')")
        return match.group(1)

    @property
    def user_id(self) -> str:
        """当前登录用户的数字 ID（从 UID cookie 前段解出）。只读。"""
        return self._user_id

    def snapshot_cookies(self) -> str:
        """拿当前完整 cookies 字符串（含服务端最新推送的 acw_tc 等临时字段）。

        上层业务应定期调用并落盘，进程重启时用最新快照初始化 SDK，避免每次首启都撞
        acw_tc 已过期需要服务端重种一次的额外往返。
        """
        return "; ".join(f"{k}={v}" for k, v in self._cookies_dict.items())

    def update_cookies(self, cookies: str) -> None:
        """整体覆盖当前 cookies（配置面板改了 cookies 后热生效）。

        cookies 必须包含合法的 UID 字段；不合法时抛 Cloud115AuthError，
        原 cookies 不被破坏。
        """
        if not cookies or "UID=" not in cookies:
            raise Cloud115AuthError("cookies missing or has no UID field")
        new_dict = self._parse_cookies(cookies)
        new_user_id = self._parse_user_id_from_dict(new_dict)   # 校验 UID 合法性
        self._cookies_dict = new_dict
        self._user_id = new_user_id

    def _merge_set_cookies(self, response: httpx.Response) -> None:
        """把响应的 Set-Cookie 头 merge 进 self._cookies_dict。

        115 服务端的 acw_tc 走 Max-Age=1800（30 分钟）阿里云 WAF token；不 merge 就
        30 分钟后被 WAF 拦截。响应 Set-Cookie 里的 Domain 属性对 115 场景不可靠
        （不带 Domain 只绑到子域），所以我们跨子域一律 merge，用真实验证过的
        "acw_tc 是账号级 token" 事实（webapi.115.com 拿 my.115.com 塞的 acw_tc 也认账）。

        只提取 `key=value` 的正文部分，忽略 `path` / `max-age` / `httponly` 等属性。
        """
        set_cookies = response.headers.get_list("set-cookie") if hasattr(response.headers, "get_list") else []
        if not set_cookies:
            return
        for line in set_cookies:
            # 取第一个 ';' 之前的 key=value 段
            head = line.split(";", 1)[0].strip()
            if not head or "=" not in head:
                continue
            key, _, value = head.partition("=")
            key = key.strip()
            if not key:
                continue
            # deleted 语义：value 为空 + 服务端塞过来通常带 Max-Age=0，我们本地也删掉
            if value == "" or value == '""':
                self._cookies_dict.pop(key, None)
            else:
                self._cookies_dict[key] = value.strip()

    async def close(self) -> None:
        # 只关掉自建的 client；外部注入的由调用方管
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "Cloud115Client":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    # ---- 5 个核心接口 ----

    async def check_cookies_alive(self) -> bool:
        """探测当前 cookie 是否仍在登录态。

        契约：只返 bool，不抛业务异常。
        alive: 200 + JSON state=True；
        dead: 302 到登录页 / JSON state=False / 非 JSON / 任何异常。

        副作用：成功响应会 merge Set-Cookie（若干 acw_tc 刷新经常从这个端点回来）。
        """
        url = f"{self._BASE_MY}/"
        params = {"ct": "guide", "ac": "status"}
        try:
            response = await self._client.get(
                url,
                params=params,
                headers=self._base_headers(),
            )
        except Exception as exc:
            # 网络错、超时、协议错等：探活语义上都算 "不 alive"，不上抛
            logger.debug("check_cookies_alive request failed: {}", exc)
            return False
        # 无论探活成功与否，只要拿到响应就 merge Set-Cookie（服务端可能塞新 acw_tc）
        async with self._cookies_lock:
            self._merge_set_cookies(response)
        # follow_redirects=False，所以 302 到登录页会体现为 status_code=302
        if response.status_code != 200:
            return False
        try:
            data = response.json()
        except Exception:
            return False
        return bool(data.get("state"))

    async def list_dir(
        self,
        cid: str = "0",
        *,
        offset: int = 0,
        limit: int = 1000,
    ) -> tuple[list[DirEntry], int]:
        """列目录一页。返回 (当前批次条目, 目录总数)。

        cid: 目录 category_id 字符串，根目录用 "0"。
        limit: 单页大小，服务端硬上限 1150；超限抛 ValueError（call site bug，不做静默截断）。
        """
        if limit > self._LIST_DIR_MAX_LIMIT:
            raise ValueError(
                f"list_dir limit {limit} exceeds server max {self._LIST_DIR_MAX_LIMIT}"
            )
        url = f"{self._BASE_WEBAPI}/files"
        params = {
            "aid": 1,
            "cid": cid,
            "offset": offset,
            "limit": limit,
            "show_dir": 1,
        }
        payload = await self._request_json("GET", url, params=params)
        if not payload.get("state"):
            # state=False 时统一按 errno 映射（990002 = 父目录不存在 -> NotFound；auth 类 -> AuthError）
            raise self._map_errno(payload, endpoint=url)
        entries = [self._parse_dir_entry(raw) for raw in (payload.get("data") or [])]
        total = int(payload.get("count", 0))
        return entries, total

    async def file_info(self, file_id: str) -> FileMeta:
        """取单文件元信息。file_id 是整数字符串（list_dir 结果里的 entry_id）。

        业务侧通常持久化的是 pickcode（跨会话稳定）而不是 file_id；如果只有 pickcode
        请用 pickcode_info。
        """
        if not file_id:
            raise ValueError("file_id is required")
        return await self._get_info(param_key="file_id", param_value=file_id, human_id=file_id)

    async def pickcode_info(self, pickcode: str) -> FileMeta:
        """按 pickcode 查文件元信息。走同一 /files/get_info 端点，只是参数名换成 pick_code。

        pickcode 是业务侧的稳定 ID；file_id 会因 115 内部存储位置变动而变化。
        """
        if not pickcode:
            raise ValueError("pickcode is required")
        return await self._get_info(param_key="pick_code", param_value=pickcode, human_id=pickcode)

    async def _get_info(self, *, param_key: str, param_value: str, human_id: str) -> FileMeta:
        url = f"{self._BASE_WEBAPI}/files/get_info"
        payload = await self._request_json("GET", url, params={param_key: param_value})
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)
        data = payload.get("data") or []
        if not data:
            raise Cloud115NotFoundError(
                f"{param_key}={human_id} not found", endpoint=url
            )
        return self._parse_file_meta(data[0])

    async def dir_info(self, cid: str) -> DirMeta:
        """取目录元信息 + 面包屑。

        cid="0" 会被 115 服务端拒（errNo=1001），SDK 层直接构造哨兵返回（name="根目录"、
        pickcode=""、paths=()），调用方不必特判。

        端点：GET webapi.115.com/category/get?cid=<cid>
        """
        if not cid:
            raise ValueError("cid is required")
        if cid == "0":
            # 根目录哨兵
            return DirMeta(
                cid="0",
                name="根目录",
                pickcode="",
                parent_id="",
                file_count=0,
                folder_count=0,
                play_long_seconds=0,
                mtime=0,
                ctime=0,
                paths=(),
            )
        url = f"{self._BASE_WEBAPI}/category/get"
        payload = await self._request_json("GET", url, params={"cid": cid})
        # category/get 的失败态：state=false + errNo=1001 参数错 / cid 不存在
        # 与 list_dir 的 state 判定风格保持一致
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)
        return self._parse_dir_meta(cid, payload)

    async def get_download_url(self, pickcode: str, user_agent: str) -> DirectUrl:
        """取 302 直链。

        user_agent 必填：115 会把它绑定进返回 URL 的 f= 指纹，调用方后续 Range GET
        必须一字不差复用同一 UA，否则 403。返回的 DirectUrl.user_agent 就是本参数。
        """
        if not pickcode:
            raise ValueError("pickcode is required")
        if not user_agent:
            raise ValueError("user_agent is required for downurl UA fingerprint binding")

        url = f"{self._BASE_PROAPI}/app/chrome/downurl"
        # payload 编码：{"pickcode": ..., "user_id": ...} -> RSA 加密 -> base64 -> form 里的 data 字段
        payload_body = {"pickcode": pickcode, "user_id": self._user_id}
        data_field = encrypt_payload(payload_body).decode("ascii")

        # downurl 请求本身的 UA 用调用方传入的 UA（服务端据此绑指纹）
        # 另需 Referer，proapi 部分场景不加会 400
        headers = self._base_headers()
        headers["User-Agent"] = user_agent
        headers["Referer"] = "https://115.com/"

        response_json = await self._request_json(
            "POST",
            url,
            data={"data": data_field},
            headers=headers,
        )
        if not response_json.get("state"):
            raise self._map_errno(response_json, endpoint=url)

        cipher_b64 = response_json.get("data")
        if not cipher_b64:
            raise Cloud115NotFoundError(
                f"downurl response missing data for pickcode {pickcode}", endpoint=url
            )
        decrypted = decrypt_response(cipher_b64)
        # 解密后是 {"<file_id>": {file_name, file_size, pick_code, sha1, url: {"url": "..."} | 0}}
        if not decrypted:
            raise Cloud115NotFoundError(
                f"downurl decrypted empty for pickcode {pickcode}", endpoint=url
            )
        # 只取第一个（chrome downurl 支持批量，但我们只传一个 pickcode）
        file_id, entry = next(iter(decrypted.items()))
        raw_url = entry.get("url")
        # url == 0 表示条目是目录、或被 115 封禁：从上层视角等同 "拿不到"
        if not isinstance(raw_url, dict):
            raise Cloud115NotFoundError(
                f"downurl refused for pickcode {pickcode} (banned or directory)",
                endpoint=url,
            )
        direct_url = raw_url.get("url", "")
        if not direct_url:
            raise Cloud115NotFoundError(
                f"downurl empty for pickcode {pickcode}", endpoint=url
            )
        return DirectUrl(
            file_id=str(file_id),
            file_name=str(entry.get("file_name", "")),
            file_size=int(entry.get("file_size", 0)),
            sha1=str(entry.get("sha1", "")),
            pickcode=str(entry.get("pick_code", pickcode)),
            url=direct_url,
            user_agent=user_agent,
            expires_at=self._parse_expires_at(direct_url),
        )

    async def get_video_info(self, pickcode: str) -> VideoInfo:
        """拿视频综合信息 + master m3u8 里的清晰度列表（VIP 专属接口）。

        流程：
          1) GET https://webapi.115.com/files/video?pickcode=... → 视频元数据 + master m3u8 URL
          2) GET master m3u8 → 解析出 variant 清晰度列表

        错误映射：
          - errno=406 "需要VIP会员" → Cloud115MembershipRequiredError
          - state=True 但 file_status != 1 → Cloud115VideoNotReadyError（转码中/失败）
          - state=True + file_status=1 但 video_url 缺 → Cloud115NotFoundError（非视频文件）
        """
        if not pickcode:
            raise ValueError("pickcode is required")
        url = f"{self._BASE_WEBAPI}/files/video"
        params = {"pickcode": pickcode}
        payload = await self._request_json("GET", url, params=params)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

        # file_status：0=转码中/未完成，1=完成。缺字段视为"完成"以兼容老响应格式。
        # 校验放在 video_url 检查之前，避免"转码中" 被误判为"非视频"（后者的语义是永久 invalid）。
        raw_status = payload.get("file_status")
        if raw_status is not None:
            try:
                file_status = int(raw_status)
            except (TypeError, ValueError):
                file_status = 1   # 无法解析时按已完成处理，避免误判
            if file_status != 1:
                raise Cloud115VideoNotReadyError(
                    f"video not ready for pickcode {pickcode} (file_status={file_status})",
                    file_status=file_status,
                    endpoint=url,
                )

        master_m3u8_url = str(payload.get("video_url", "") or "")
        if not master_m3u8_url:
            raise Cloud115NotFoundError(
                f"video_url missing for pickcode {pickcode} (not a video)",
                endpoint=url,
            )

        # 拉 master m3u8 解析清晰度
        master_text = await self._get_text(master_m3u8_url)
        definitions = self._parse_master_m3u8(master_text, base_url=master_m3u8_url)

        return VideoInfo(
            pickcode=pickcode,
            width=int(payload.get("width") or 0),
            height=int(payload.get("height") or 0),
            thumb_url=str(payload.get("thumb_url", "") or ""),
            master_m3u8_url=master_m3u8_url,
            definitions=definitions,
        )

    async def get_video_segments(
        self,
        pickcode: str,
        *,
        prefer_bandwidth: int | None = None,
    ) -> list[VideoSegment]:
        """拿指定/最高清晰度的 HLS ts 分段列表（VIP 专属接口）。

        prefer_bandwidth：想要的清晰度码率（bit/s）。None 时挑最高码率。
        找不到匹配码率时也回退到最高码率（不静默返回错误，因为清晰度筛选是"偏好"）。

        每个 VideoSegment 是一个独立可解码的 ts URL + 时长；上层直接
        `ffmpeg -i <url> -ss 0 -vframes 1` 抽帧即可，无需 seek。
        """
        info = await self.get_video_info(pickcode)
        if not info.definitions:
            raise Cloud115NotFoundError(
                f"no video definitions available for pickcode {pickcode}",
                endpoint=info.master_m3u8_url,
            )
        variant = self._pick_variant(info.definitions, prefer_bandwidth)
        variant_text = await self._get_text(variant.m3u8_url)
        return self._parse_variant_m3u8(variant_text, base_url=variant.m3u8_url)

    # ---- 离线下载 ----
    #
    # 端点选型：全部走 https://115.com/web/lixian/?ct=lixian&ac=<action> 明文端点。
    # 加密备用端点 https://clouddownload.115.com/lixianssp/?ac=<action> 复用 cipher.py 可用，
    # 但目前明文足以覆盖，避免不必要的复杂度。
    #
    # 配额：非 VIP 5 次/月，VIP 200 次/月。每提交 1 条 add 扣 1 次，配额用尽 → Cloud115OfflineQuotaExceededError。
    # 任务完成后 file_id/pickcode 有值，直接可接 pickcode_info / get_download_url。

    _LIXIAN_URL = "https://115.com/web/lixian/"
    _OFFLINE_SPACE_URL = "https://115.com/"     # ?ct=offline&ac=space 拿全局离线目录大小配额（本 SDK 不暴露）
    _OFFLINE_DOWNPATH_URL = "https://webapi.115.com/offine/downpath"   # 115 端点名字确实少个 l（不是 offline）

    # 离线 BT（选文件）：sample 上传 .torrent 拿 pickcode/sha1（免 userkey 签名），
    # 再走上面 _LIXIAN_URL 的 ac=torrent 解析文件列表、ac=add_task_bt 按选中 index 建任务。
    _SAMPLE_INIT_UPLOAD_URL = "https://uplb.115.com/3.0/sampleinitupload.php"

    async def list_offline_tasks(
        self,
        *,
        page: int = 1,
        page_size: int = 30,
    ) -> OfflineTaskPage:
        """分页列离线任务。

        page: 从 1 开始。page_size 服务端默认 30，上限没实测；实用范围 10-50。
        返回按 add_time 倒序（最新添加的在前）。
        """
        if page < 1:
            raise ValueError(f"page must be >= 1, got {page}")
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        payload = await self._request_json(
            "GET",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "task_lists", "page": page, "page_size": page_size},
        )
        # task_lists 成功时不返 state 字段（响应直接是数据），失败时才有 state=false + errno
        if payload.get("state") is False:
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)
        tasks_raw = payload.get("tasks") or []
        return OfflineTaskPage(
            page=int(payload.get("page") or page),
            page_count=int(payload.get("page_count") or 1),
            page_size=int(payload.get("page_size") or page_size),
            total_tasks=int(payload.get("total") or 0),
            tasks=[self._parse_offline_task(raw) for raw in tasks_raw],
        )

    async def offline_quota(self) -> OfflineQuota:
        """拿本月离线下载次数配额。返回 total（月配额）+ remaining（剩余次数）。

        实现：从 task_lists 的第 1 页响应里读 total/quota 字段（走同一端点 -> 减少一次请求）。
        避免走 lixianssp 加密端点。
        """
        payload = await self._request_json(
            "GET",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "task_lists", "page": 1, "page_size": 1},
        )
        if payload.get("state") is False:
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)
        # 实测响应字段：total=200（月配额），quota=剩余次数
        return OfflineQuota(
            total=int(payload.get("total") or 0),
            remaining=int(payload.get("quota") or 0),
        )

    async def default_download_dir(self) -> DirEntry:
        """拿"云下载"默认保存目录信息。返回一个 DirEntry（is_dir=True）。

        115 服务端可以同时配多个候选目录，本方法返回其中 `is_selected=1` 的那一个。
        用途：上层 UI 在"新建离线任务"时预填 save_dir_id，或作为默认值兜底。
        """
        payload = await self._request_json("GET", self._OFFLINE_DOWNPATH_URL)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=self._OFFLINE_DOWNPATH_URL)
        candidates = payload.get("data") or []
        # 挑 is_selected=1 的；如果都没标就取第一个
        selected = next(
            (c for c in candidates if str(c.get("is_selected", "")) == "1"),
            candidates[0] if candidates else None,
        )
        if selected is None:
            raise Cloud115NotFoundError(
                "no default cloud download dir configured",
                endpoint=self._OFFLINE_DOWNPATH_URL,
            )
        # DirEntry 结构对齐：目录 entry_id = file_id
        return DirEntry(
            entry_id=str(selected.get("file_id", "")),
            parent_id="",
            name=str(selected.get("file_name", "")),
            is_dir=True,
            size=0,
            sha1=None,
            pickcode="",
            mtime=int(selected.get("update_time") or 0),
            ctime=0,
            is_video=False,
        )

    async def add_offline_urls(
        self,
        urls: list[str],
        *,
        save_dir_id: str,
    ) -> list[OfflineTaskAddResult]:
        """批量提交离线下载任务。

        urls: 支持 http://, https://, ftp://, magnet:?xt=urn:btih:..., ed2k://。
              空列表抛 ValueError；单条空 URL 会被服务端拒绝但不预校验（115 自己有格式检查）。
        save_dir_id: 保存到的目录 cid（必填）。用 default_download_dir().entry_id 拿默认目录。

        返回：每个 URL 对应的 OfflineTaskAddResult（含 info_hash + 原 URL）。批量提交时
        个别 URL 失败（比如无效磁力）也不会整体失败，失败项 info_hash 为空串。

        配额相关：每条 URL 扣 1 次月配额；配额用尽抛 Cloud115OfflineQuotaExceededError
        且**整批都不生效**（服务端事务性拒绝）。
        """
        if not urls:
            raise ValueError("urls must not be empty")
        if not save_dir_id:
            raise ValueError("save_dir_id is required")

        data: dict[str, Any] = {"wp_path_id": save_dir_id}
        for i, url in enumerate(urls):
            data[f"url[{i}]"] = url

        payload = await self._request_json(
            "POST",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "add_task_urls"},
            data=data,
        )
        if payload.get("state") is False:
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)

        # 成功响应结构：{"state":true, "errno":0, "errcode":0, "result":[{info_hash, url}, ...]}
        results_raw = payload.get("result") or []
        return [
            OfflineTaskAddResult(
                info_hash=str(item.get("info_hash", "")),
                url=str(item.get("url", "")),
            )
            for item in results_raw
        ]

    async def delete_offline_tasks(
        self,
        info_hashes: list[str],
        *,
        delete_source_files: bool = False,
    ) -> None:
        """批量删除离线任务（不管是否已完成）。

        info_hashes: 空列表抛 ValueError。
        delete_source_files: True 时同时把云盘里已下载的文件也删掉（不可逆！）。
        """
        if not info_hashes:
            raise ValueError("info_hashes must not be empty")
        data: dict[str, Any] = {"flag": "1" if delete_source_files else "0"}
        for i, ih in enumerate(info_hashes):
            data[f"hash[{i}]"] = ih
        payload = await self._request_json(
            "POST",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "task_del"},
            data=data,
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)

    async def clear_offline_tasks(self, scope: ClearScope = "finished") -> None:
        """按范围清空离线任务列表。

        scope 取值：
          - "finished"            清已完成
          - "failed"              清已失败
          - "running"             清进行中（会中断任务！）
          - "all"                 全部
          - "finished_with_source"  清已完成 + 删源文件
          - "all_with_source"       全部 + 删源文件
        """
        if scope not in _CLEAR_SCOPE_TO_FLAG:
            raise ValueError(
                f"unknown scope {scope!r}; expected one of {sorted(_CLEAR_SCOPE_TO_FLAG)}"
            )
        flag = _CLEAR_SCOPE_TO_FLAG[scope]
        payload = await self._request_json(
            "POST",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "task_clear"},
            data={"flag": str(flag)},
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)

    async def restart_offline_task(self, info_hash: str) -> None:
        """重试一条失败/停滞的离线任务。

        对已完成任务无效（服务端会 state=false）。上层可以先 list 出 status=-1 的再批量 restart。
        """
        if not info_hash:
            raise ValueError("info_hash is required")
        payload = await self._request_json(
            "POST",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "restart"},
            data={"info_hash": info_hash},
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)

    # ---- 离线 BT（种子 + 选文件）----
    #
    # 三步（全程真机验证，2026-07-12）：
    #   1. upload_torrent(bytes)          sample 上传 .torrent -> (pickcode, sha1)，免 userkey 签名
    #   2. torrent_info(pickcode, sha1)   ac=torrent -> info_hash + 文件列表（每项带默认勾选态）
    #   3. add_task_bt(info_hash, wanted) ac=add_task_bt -> 只下选中的文件
    # 选择由中间的 torrent_info 拿到列表、上层交互勾选、再把选中 index 交给 add_task_bt 完成，
    # SDK 只提供这三个原语，不做"一步到位"封装（选择本身需要一次人机交互往返）。

    async def upload_torrent(self, data: bytes, *, save_dir_id: str) -> tuple[str, str]:
        """上传一个 .torrent 文件，返回 (pickcode, sha1)，供 torrent_info 解析。

        走 115 的 sample 上传（**免 userkey 签名**）：先 sampleinitupload 拿阿里云 OSS
        PostObject 表单，再把种子字节 POST 到 OSS，115 回调直接返回 pickcode + sha1。

        data: .torrent 文件原始字节（不能为空）。
        save_dir_id: 种子文件的落脚目录 cid。纯落脚点——选文件只认 pickcode/sha1，与存哪无关；
            可用 default_download_dir().entry_id 或一个专门的种子暂存目录。
            注意：种子文件会**留在**该目录，SDK 不自动清理，需要的话上层自行删。

        失败：初始化缺字段 / OSS 非 200 / 回调无 pickcode -> Cloud115RequestError。
        """
        if not data:
            raise ValueError("torrent data must not be empty")
        if not save_dir_id:
            raise ValueError("save_dir_id is required")

        # 1) 初始化：拿 OSS PostObject 表单（115 host，走带 cookies 的通道）
        form = await self._request_json(
            "POST",
            self._SAMPLE_INIT_UPLOAD_URL,
            data={
                "userid": self._user_id,
                "filename": "upload.torrent",
                "filesize": str(len(data)),
                "target": f"U_1_{save_dir_id}",
            },
        )
        required = ("host", "object", "accessid", "policy", "signature", "callback")
        if not all(form.get(k) for k in required):
            raise Cloud115RequestError(
                "sample upload init missing OSS fields",
                method="POST",
                url=self._SAMPLE_INIT_UPLOAD_URL,
                detail=str(form)[:200],
            )

        # 2) 把种子字节 POST 到阿里云 OSS（外部 host，不带 115 cookies；success_action_status=200
        #    让 OSS 直接回 200 + 115 回调体，避免 302；file 字段放最后是 OSS PostObject 的要求）
        oss_resp = await self._client.post(
            form["host"],
            data={
                "key": form["object"],
                "policy": form["policy"],
                "OSSAccessKeyId": form["accessid"],
                "success_action_status": "200",
                "signature": form["signature"],
                "callback": form["callback"],
            },
            files={"file": ("upload.torrent", data, "application/octet-stream")},
        )
        if oss_resp.status_code != 200:
            raise Cloud115RequestError(
                f"OSS upload failed http {oss_resp.status_code}",
                method="POST",
                url=str(form["host"]),
                detail=oss_resp.text[:200],
            )
        try:
            body = oss_resp.json()
        except Exception as exc:
            raise Cloud115RequestError(
                "OSS upload returned non-json body",
                method="POST",
                url=str(form["host"]),
                detail=oss_resp.text[:200],
            ) from exc
        file_data = body.get("data") or {}
        pickcode = str(file_data.get("pick_code") or file_data.get("pickcode") or "")
        sha1 = str(file_data.get("sha1") or "")
        if not pickcode or not sha1:
            raise Cloud115RequestError(
                "OSS upload callback missing pickcode/sha1",
                method="POST",
                url=str(form["host"]),
                detail=str(body)[:200],
            )
        return pickcode, sha1

    async def torrent_info(self, pickcode: str, sha1: str) -> TorrentInfo:
        """用已上传种子的 pickcode + sha1 解析出 info_hash 和文件列表。

        pickcode/sha1 来自 upload_torrent。返回的 files 每项带 115 默认勾选态 wanted，
        上层展示给用户勾选，最终把选中的 index 列表交给 add_task_bt。
        """
        if not pickcode:
            raise ValueError("pickcode is required")
        if not sha1:
            raise ValueError("sha1 is required")
        payload = await self._request_json(
            "POST",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "torrent"},
            data={"pickcode": pickcode, "sha1": sha1},
        )
        if payload.get("state") is False:
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)
        filelist = payload.get("torrent_filelist_web") or []
        files = [
            TorrentFileEntry(
                index=i,
                path=str(item.get("path", "")),
                size=int(item.get("size") or 0),
                wanted=bool(item.get("wanted")),
            )
            for i, item in enumerate(filelist)
        ]
        return TorrentInfo(
            info_hash=str(payload.get("info_hash", "")),
            name=str(payload.get("torrent_name", "")),
            file_count=int(payload.get("file_count") or len(files)),
            files=files,
        )

    async def add_task_bt(
        self,
        info_hash: str,
        wanted: list[int],
        *,
        save_dir_id: str,
        savepath: str,
    ) -> str:
        """按选中的文件创建一个 BT 离线任务，返回任务 info_hash。

        info_hash: 来自 torrent_info。
        wanted: 选中要下载的文件 **index 列表**（对应 torrent_info().files[i].index），
            SDK 内部拼成逗号串。空列表抛 ValueError（115 不接受"一个都不选"）。
        save_dir_id: 保存目录 cid（wp_path_id），任务在其下新建 savepath 文件夹。
        savepath: 新建的顶层文件夹名，通常用 TorrentInfo.name。

        扣 1 次月配额。行为要点（均真机实测）：
        - 相同 info_hash 已在离线列表 -> Cloud115OfflineTaskExistsError（errcode=10008，良性重复，
          不扣配额）；要改选择须先 delete_offline_tasks 删旧任务再重加。
        - 建成后 list_offline_tasks 里那条的 size 是**整个种子**大小占位、不反映本次选择，
          但实际只会下载选中的文件（传 wanted=[1] 实测只落地索引 1 那个文件）。
        - 内容已在 115 服务端时会秒完成（status=2），不占用户带宽。
        """
        if not info_hash:
            raise ValueError("info_hash is required")
        if not wanted:
            raise ValueError("wanted must not be empty (115 rejects a task with no file selected)")
        if not save_dir_id:
            raise ValueError("save_dir_id is required")
        if not savepath:
            raise ValueError("savepath is required")

        payload = await self._request_json(
            "POST",
            self._LIXIAN_URL,
            params={"ct": "lixian", "ac": "add_task_bt"},
            data={
                "info_hash": info_hash,
                "wanted": ",".join(str(i) for i in wanted),
                "savepath": savepath,
                "wp_path_id": save_dir_id,
            },
        )
        if payload.get("state") is False:
            # "任务已存在"落在 errcode（不是 errno），且数字 10008 与配额 errno 撞号 -> 单独判，
            # 别经 _map_errno 误映射成 Cloud115OfflineQuotaExceededError。
            errcode = payload.get("errcode")
            msg = payload.get("error_msg") or payload.get("error") or "unknown"
            if errcode == 10008 or "已存在" in str(msg):
                raise Cloud115OfflineTaskExistsError(
                    f"{msg} (errcode={errcode})",
                    info_hash=str(payload.get("info_hash") or info_hash),
                    errno=errcode if isinstance(errcode, int) else None,
                    endpoint=self._LIXIAN_URL,
                )
            raise self._map_errno(payload, endpoint=self._LIXIAN_URL)
        return str(payload.get("info_hash") or info_hash)

    # ---- 内部工具 ----

    def _base_headers(self) -> dict[str, str]:
        # Cookie 从内部 dict 动态拼串（保序，便于 acw_tc 等被服务端 Set-Cookie 更新后透传）
        return {
            "Cookie": self.snapshot_cookies(),
            "User-Agent": self._user_agent,
        }

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """带退避重试的 HTTP 请求，返回 2xx httpx.Response。上层再自行 .json() 或 .text。

        - 429：直接抛 Cloud115RateLimitedError（不重试，避免账号信号升级）
        - 5xx / TimeoutException / NetworkError：最多 2 次退避重试
        - 4xx（401/403）：401/403 → Cloud115AuthError；其它 → Cloud115RequestError

        每次成功响应到达后会 merge Set-Cookie 到内部 cookies dict（保活 acw_tc 等）。
        401/403 抛异常前也会 merge（可能带着新 acw_tc / logout 信号）。
        """
        last_error: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
            # 每次重试前重新拼 headers（因为上一次响应可能 merge 了新的 acw_tc）
            request_headers = self._base_headers()
            if headers:
                request_headers.update(headers)
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=request_headers,
                )
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                logger.warning(
                    "cloud115 request transient error method={} url={} attempt={}/{} detail={}",
                    method, url, attempt + 1, self._MAX_RETRIES + 1, exc,
                )
                if attempt >= self._MAX_RETRIES:
                    break
                await asyncio.sleep(self._RETRY_BACKOFF_STEP * (attempt + 1))
                continue

            # 无论后续是否抛异常，Set-Cookie 都可以 merge（服务端可能同时塞新 acw_tc + 拒绝请求）
            async with self._cookies_lock:
                self._merge_set_cookies(response)

            status = response.status_code
            if status == 429:
                # 限流：立刻抛，携带 Retry-After（不做重试，避免账号触发更严格的风控）
                retry_after = response.headers.get("Retry-After")
                retry_after_int = int(retry_after) if retry_after and retry_after.isdigit() else None
                raise Cloud115RateLimitedError(
                    f"429 rate limited on {method} {url}",
                    retry_after_seconds=retry_after_int,
                )
            if status in (401, 403):
                raise Cloud115AuthError(
                    f"http {status} on {method} {url}", endpoint=url
                )
            if 500 <= status < 600:
                # 5xx：退避重试
                last_error = Cloud115RequestError(
                    f"http {status} on {method} {url}",
                    method=method,
                    url=url,
                    detail=response.text[:200],
                )
                logger.warning(
                    "cloud115 5xx method={} url={} status={} attempt={}/{}",
                    method, url, status, attempt + 1, self._MAX_RETRIES + 1,
                )
                if attempt >= self._MAX_RETRIES:
                    break
                await asyncio.sleep(self._RETRY_BACKOFF_STEP * (attempt + 1))
                continue
            if not (200 <= status < 300):
                raise Cloud115RequestError(
                    f"http {status} on {method} {url}",
                    method=method,
                    url=url,
                    detail=response.text[:200],
                )
            return response

        # 重试耗尽
        detail = str(last_error) if last_error else "unknown"
        raise Cloud115RequestError(
            f"request failed after {self._MAX_RETRIES + 1} attempts: {method} {url} ({detail})",
            method=method,
            url=url,
            detail=detail,
        ) from last_error

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """_request 的 JSON 便捷封装。2xx 但非 JSON 抛 Cloud115RequestError。"""
        response = await self._request(method, url, params=params, data=data, headers=headers)
        try:
            return response.json()
        except Exception as exc:
            raise Cloud115RequestError(
                f"non-json body on {method} {url}",
                method=method,
                url=url,
                detail=str(exc),
            ) from exc

    async def _get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        """GET url 返回文本内容（m3u8 场景专用，不做 JSON 解析）。"""
        response = await self._request("GET", url, headers=headers)
        return response.text

    @staticmethod
    def _map_errno(payload: dict[str, Any], *, endpoint: str) -> Cloud115Error:
        """把 state=False 的响应按 errno 映射到具体异常子类。"""
        errno = payload.get("errno") or payload.get("errNo") or payload.get("code")
        message = (
            payload.get("error")
            or payload.get("error_msg")
            or payload.get("message")
            or payload.get("msg")
            or "unknown"
        )
        try:
            errno_int = int(errno) if errno is not None else None
        except (TypeError, ValueError):
            errno_int = None

        if errno_int in _AUTH_ERRNOS:
            return Cloud115AuthError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _MEMBERSHIP_REQUIRED_ERRNOS:
            return Cloud115MembershipRequiredError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _OFFLINE_QUOTA_EXCEEDED_ERRNOS:
            return Cloud115OfflineQuotaExceededError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _NOT_FOUND_ERRNOS:
            return Cloud115NotFoundError(
                f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
            )
        if errno_int in _REQUEST_ERRNOS:
            return Cloud115RequestError(
                f"{message} (errno={errno_int})",
                method=None,
                url=endpoint,
                detail=message,
                errno=errno_int,
            )
        # 未识别的 errno：不静默吞，回到基类让上层看到
        return Cloud115Error(
            f"{message} (errno={errno_int})", errno=errno_int, endpoint=endpoint
        )

    @staticmethod
    def _parse_dir_entry(raw: dict[str, Any]) -> DirEntry:
        """115 短字段名 -> DirEntry。fid 存在 => 文件，缺失 => 目录。"""
        is_dir = "fid" not in raw
        if is_dir:
            # 目录的 category_id 是自己的 cid，parent 是 pid
            entry_id = str(raw.get("cid", ""))
            parent_id = str(raw.get("pid", ""))
        else:
            # 文件的 file_id 是 fid，parent 是 cid
            entry_id = str(raw.get("fid", ""))
            parent_id = str(raw.get("cid", ""))
        return DirEntry(
            entry_id=entry_id,
            parent_id=parent_id,
            name=str(raw.get("n", "")),
            is_dir=is_dir,
            size=int(raw.get("s") or 0),
            sha1=str(raw["sha"]) if raw.get("sha") else None,
            pickcode=str(raw.get("pc", "")),
            mtime=int(raw.get("te") or 0),
            ctime=int(raw.get("tp") or 0),
            is_video=bool(raw.get("iv")) if not is_dir else False,
        )

    @staticmethod
    def _parse_file_meta(raw: dict[str, Any]) -> FileMeta:
        """get_info 单条 -> FileMeta。字段与目录条目短名一致但只覆盖文件字段。"""
        return FileMeta(
            file_id=str(raw.get("fid") or raw.get("file_id") or ""),
            parent_id=str(raw.get("cid", "")),
            name=str(raw.get("n", "")),
            size=int(raw.get("s") or 0),
            sha1=str(raw.get("sha", "")),
            pickcode=str(raw.get("pc", "")),
            mtime=int(raw.get("te") or 0),
            ctime=int(raw.get("tp") or 0),
            is_video=bool(raw.get("iv")),
        )

    @staticmethod
    def _parse_offline_task(raw: dict[str, Any]) -> OfflineTask:
        """task_lists 单条 -> OfflineTask。字段名对齐 2026-07-12 观察的真实响应。"""
        # percentDone 服务端一般是 0-100 的数字（可能 int 或 float）
        try:
            percent = float(raw.get("percentDone") or raw.get("display_percent") or 0)
        except (TypeError, ValueError):
            percent = 0.0
        return OfflineTask(
            info_hash=str(raw.get("info_hash", "")),
            name=str(raw.get("name", "")),
            size=int(raw.get("size") or 0),
            status=int(raw.get("status") if raw.get("status") is not None else 0),
            status_text=str(raw.get("status_text", "") or raw.get("display_status", "")),
            percent_done=percent,
            rate_download=int(raw.get("rateDownload") or 0),
            peers=int(raw.get("peers") or 0),
            left_time_seconds=int(raw.get("left_time") or 0),
            add_time=int(raw.get("add_time") or 0),
            last_update=int(raw.get("last_update") or 0),
            file_id=str(raw.get("file_id", "") or ""),
            pickcode=str(raw.get("pick_code", "") or ""),
            save_dir_id=str(raw.get("wp_path_id", "") or ""),
            source_url=str(raw.get("url", "") or ""),
            retry_count=int(raw.get("retry_count") or 0),
            retry_limit=int(raw.get("retry_limit") or 0),
        )

    @staticmethod
    def _parse_dir_meta(cid: str, payload: dict[str, Any]) -> DirMeta:
        """/category/get 响应 -> DirMeta。

        字段名（观察自 2026-07-12 真实响应）：
            file_name / pick_code / paths[] / count / folder_count / play_long / ctime / utime
        parent_id 从 paths 末尾解析；paths 是从根目录到当前目录父级的面包屑链。
        """
        raw_paths = payload.get("paths") or []
        crumbs: list[DirBreadcrumb] = []
        for item in raw_paths:
            if not isinstance(item, dict):
                continue
            # 根目录 file_id 是数字 0，不能用 `x or ""` 吞掉；显式挑存在的字段
            fid_raw = item.get("file_id") if "file_id" in item else item.get("cid")
            name_raw = item.get("file_name") if "file_name" in item else item.get("name")
            crumbs.append(
                DirBreadcrumb(
                    file_id="" if fid_raw is None else str(fid_raw),
                    name="" if name_raw is None else str(name_raw),
                )
            )
        # 父目录 cid 从面包屑末尾拿；如果 paths 为空（少见）则空串
        parent_id = crumbs[-1].file_id if crumbs else ""
        return DirMeta(
            cid=cid,
            name=str(payload.get("file_name", "") or ""),
            pickcode=str(payload.get("pick_code", "") or ""),
            parent_id=parent_id,
            file_count=int(payload.get("count") or 0),
            folder_count=int(payload.get("folder_count") or 0),
            play_long_seconds=int(payload.get("play_long") or 0),
            mtime=int(payload.get("utime") or 0),
            ctime=int(payload.get("ctime") or 0),
            paths=tuple(crumbs),
        )

    @staticmethod
    def _parse_expires_at(direct_url: str) -> int:
        """从直链的 t=<unix_ts> query 参数解出过期时间；缺失或非法返回 -1。"""
        try:
            query = urlsplit(direct_url).query
            for key, value in parse_qsl(query):
                if key == "t" and value.isdigit():
                    return int(value)
        except Exception:
            pass
        return -1

    # ---- m3u8 解析工具 ----

    # #EXT-X-STREAM-INF:<attrs> 里的属性抓取（BANDWIDTH / RESOLUTION / NAME 等）
    _M3U8_ATTR_PATTERN = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))')

    @classmethod
    def _parse_master_m3u8(cls, text: str, *, base_url: str) -> list[VideoDefinition]:
        """解析 HLS master playlist。相对 URL 用 base_url 拼绝对。

        典型输入：
            #EXTM3U
            #EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1800000,RESOLUTION=1280x720,NAME="HD"
            https://.../variant.m3u8
        """
        definitions: list[VideoDefinition] = []
        pending_attrs: dict[str, str] | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-STREAM-INF:"):
                # 记录下一非注释行的 variant 属性
                attrs_str = line[len("#EXT-X-STREAM-INF:") :]
                pending_attrs = {}
                for match in cls._M3U8_ATTR_PATTERN.finditer(attrs_str):
                    key = match.group(1)
                    value = match.group(2) if match.group(2) is not None else match.group(3)
                    pending_attrs[key] = value
                continue
            if line.startswith("#"):
                continue
            # 非注释、非空 → 是上一 STREAM-INF 声明的 variant URL
            attrs = pending_attrs or {}
            pending_attrs = None
            try:
                bandwidth = int(attrs.get("BANDWIDTH", "0") or "0")
            except ValueError:
                bandwidth = 0
            definitions.append(
                VideoDefinition(
                    bandwidth=bandwidth,
                    resolution=attrs.get("RESOLUTION", ""),
                    label=attrs.get("NAME", ""),
                    m3u8_url=urljoin(base_url, line),
                )
            )
        return definitions

    @classmethod
    def _parse_variant_m3u8(cls, text: str, *, base_url: str) -> list[VideoSegment]:
        """解析 HLS variant playlist（含具体 ts 段）。相对 URL 用 base_url 拼绝对。

        典型输入：
            #EXTM3U
            #EXTINF:10.000000,
            /a865.../seg-00001.ts?u=...
            #EXTINF:9.99,
            /b59d.../seg-00002.ts?u=...
            #EXT-X-ENDLIST
        """
        segments: list[VideoSegment] = []
        pending_duration: float | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                # 格式：#EXTINF:10.000000,  → 逗号前是时长
                payload = line[len("#EXTINF:") :].split(",", 1)[0]
                try:
                    pending_duration = float(payload)
                except ValueError:
                    pending_duration = None
                continue
            if line.startswith("#"):
                continue
            # 非注释、非空 → ts URL
            duration = pending_duration if pending_duration is not None else 0.0
            pending_duration = None
            segments.append(
                VideoSegment(
                    index=len(segments),
                    url=urljoin(base_url, line),
                    duration_seconds=duration,
                )
            )
        return segments

    @staticmethod
    def _pick_variant(
        definitions: list[VideoDefinition],
        prefer_bandwidth: int | None,
    ) -> VideoDefinition:
        """按偏好挑一个 variant：找不到匹配码率时回退到最高码率。"""
        if prefer_bandwidth is not None:
            exact = next((d for d in definitions if d.bandwidth == prefer_bandwidth), None)
            if exact is not None:
                return exact
        # fallback：最高码率
        return max(definitions, key=lambda d: d.bandwidth)
