"""115 网盘极简异步客户端。

覆盖播放/查找/缩略图上层需求的 HTTP 接口：
    - check_cookies_alive
    - list_dir
    - file_info
    - get_download_url          （非会员亦可，但直链限速 100KB/s + CDN 多请求拉黑）
    - get_video_info            （VIP 专属：拿视频 master m3u8 + 清晰度列表）
    - get_video_segments        （VIP 专属：拿指定/最高清晰度的 HLS ts 分段列表）
    - 构造函数（cookies 字符串 -> httpx.AsyncClient）

不含：二维码登录、离线下载、上传、分享、事件订阅、图片 CDN 等。
不依赖任何业务 model / service / schema，纯 HTTP + RSA 层。
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from loguru import logger

from src.lib.cloud115.cipher import decrypt_response, encrypt_payload
from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
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
        self._cookies = cookies
        self._user_id = self._parse_user_id(cookies)
        self._user_agent = user_agent or self._DEFAULT_UA
        # 外部注入的 client 由调用方负责关闭（不做 owned/borrowed 引用计数简化状态）
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,          # 忽略环境代理，避免流量意外走公司/系统代理
            follow_redirects=False,   # check_cookies_alive 靠 302 判死活；downurl 也不能自动跟 302
        )

    # ---- 构造与生命周期 ----

    @classmethod
    def _parse_user_id(cls, cookies: str) -> str:
        match = cls._UID_PATTERN.search(cookies)
        if not match:
            raise Cloud115AuthError("UID missing or malformed (expected 'UID=<int>_A1_<ts>')")
        return match.group(1)

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
        """取单文件元信息。file_id 是整数字符串（list_dir 结果里的 entry_id）。"""
        url = f"{self._BASE_WEBAPI}/files/get_info"
        params = {"file_id": file_id}
        payload = await self._request_json("GET", url, params=params)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)
        data = payload.get("data") or []
        if not data:
            # state=True 但 data 空 = file_id 无效
            raise Cloud115NotFoundError(
                f"file_id {file_id} not found", endpoint=url
            )
        return self._parse_file_meta(data[0])

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

        errno=406 "需要VIP会员" → Cloud115MembershipRequiredError
        """
        if not pickcode:
            raise ValueError("pickcode is required")
        url = f"{self._BASE_WEBAPI}/files/video"
        params = {"pickcode": pickcode}
        payload = await self._request_json("GET", url, params=params)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

        master_m3u8_url = str(payload.get("video_url", "") or "")
        if not master_m3u8_url:
            raise Cloud115NotFoundError(
                f"video_url missing for pickcode {pickcode} (not a video or未转码?)",
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

    # ---- 内部工具 ----

    def _base_headers(self) -> dict[str, str]:
        # Cookie 逐字节透传，不用 httpx cookies= 参数（避免重排、破坏服务端签名）
        return {
            "Cookie": self._cookies,
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
        """
        request_headers = self._base_headers()
        if headers:
            request_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(self._MAX_RETRIES + 1):
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
        message = payload.get("error") or payload.get("message") or payload.get("msg") or "unknown"
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
