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
    - iter_files_recursive       （递归枚举目录树全部文件；play_long/ic 白给）
    - copy_files / move_files    （云端零流量搬运；copy 产新 fid/pickcode，move 不变）
    - batch_rename / delete_files（批量改名 / 删除进回收站）
    - download_bytes             （小文件下载：字幕等；封装同 UA 约束）
    - snapshot_cookies / update_cookies （cookies 保活/热替换，见 _merge_set_cookies）
    - rapid_upload                （本地文件仅尝试秒传，不回退普通上传）
    - 构造函数（cookies 字符串 -> httpx.AsyncClient）

不含：二维码登录（在独立的 qrlogin.Cloud115QrLogin，登录发生在拿到 cookies 之前）、
普通文件上传、分享、事件订阅、图片 CDN 等。
不依赖任何业务 model / service / schema，纯 HTTP + RSA 层。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from loguru import logger

from src.lib.cloud115.cipher import (
    decrypt_response,
    decrypt_upload_response,
    encrypt_payload,
    make_upload_payload,
)
from src.lib.cloud115.exceptions import (
    Cloud115AuthError,
    Cloud115Error,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115OfflineQuotaExceededError,
    Cloud115OfflineTaskExistsError,
    Cloud115RateLimitedError,
    Cloud115RequestError,
    Cloud115RiskControlError,
    Cloud115VideoNotReadyError,
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
    VideoDefinition,
    VideoInfo,
    VideoSegment,
)


# 归类到具体异常子类的 errno 集合。全部来自 p115client 观察 + 反向验证。
# 认证类：session 失效 / 冻结 / 需短信验证。
# errno=99 的服务端文案是“请重新登录”，p115client 也映射为登录态失效；保持 AuthError，
# 不把一次 downurl 高频场景的观察泛化成限流错误（115 未公开该 errno 的稳定限流契约）。
_AUTH_ERRNOS: frozenset[int] = frozenset({
    50003, 50004, 99, 911, 20130827, 99999, 990009, 990017,
})
# 未找到类：文件/目录不存在 / pickcode 无效 / 资源被封禁。
_NOT_FOUND_ERRNOS: frozenset[int] = frozenset({
    20121, 20125, 990002, 4100003, 4100008,
})
# 请求参数错误：调用方 bug，不是重试能解决的。
_REQUEST_ERRNOS: frozenset[int] = frozenset({990005})
# 需要 VIP 会员：上游以 errno=406 拒绝当前账号的操作。
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
    _BASE_UPLOAD = "https://uplb.115.com"
    _UPLOAD_APP_VERSION_URL = "https://appversion.115.com/1.0/web/1.0/api/getMultiVer"

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
    _UID_SSOENT_PATTERN = re.compile(r"^\d+_([A-Z]\d)_")
    _RAPID_UPLOAD_PROTOCOL_BY_SSOENT = {
        "F1": "android",
        "R2": "web",
    }

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
        min_request_interval: float = 0.0,
    ) -> None:
        if not cookies or "UID=" not in cookies:
            # cookies 缺失或不含 UID：SDK 层直接判死，不做延迟报错
            raise Cloud115AuthError("cookies missing or has no UID field")
        # cookies 存成 dict（保序）便于响应到达后 merge 服务端 Set-Cookie 更新
        self._cookies_dict: dict[str, str] = self._parse_cookies(cookies)
        self._user_id = self._parse_user_id_from_dict(self._cookies_dict)
        self._cookies_lock = asyncio.Lock()
        self._upload_userkeys: dict[str, str] = {}
        self._upload_userkey_lock = asyncio.Lock()
        self._upload_app_version: str | None = None
        self._upload_app_version_lock = asyncio.Lock()
        self._user_agent = user_agent or self._DEFAULT_UA
        # 全局请求限速：相邻请求最小间隔（秒），0 表示不限速。对标 AList 115 驱动的
        # limit_rate——把所有 API 请求匀速化，避免每条 item 的瞬时突发越过 webapi 前置
        # 阿里云 WAF 的 ~1-2 r/s 风控阈值。批量秒传场景由上层传入（如 1.0 = 1 r/s）。
        self._min_request_interval = max(0.0, min_request_interval)
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0  # monotonic 时钟下允许发起下次请求的最早时刻
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

    @property
    def user_agent(self) -> str:
        """SDK 对 115 发请求时使用的 UA；HLS URL 消费方必须原样复用。"""
        return self._user_agent

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
        # Cookie 槽位或账号可能已切换，旧 userkey 不能继续复用。
        self._upload_userkeys.clear()

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

    async def probe_cookies_status(self) -> Cloud115CookieStatus:
        """探测登录态，并区分明确失效与临时上游不可用。"""
        url = f"{self._BASE_MY}/"
        params = {"ct": "guide", "ac": "status"}
        try:
            response = await self._client.get(
                url,
                params=params,
                headers=self._base_headers(),
            )
        except Exception as exc:
            logger.debug("probe_cookies_status request failed: {}", exc)
            return Cloud115CookieStatus.UNAVAILABLE
        # 无论探活成功与否，只要拿到响应就 merge Set-Cookie（服务端可能塞新 acw_tc）
        async with self._cookies_lock:
            self._merge_set_cookies(response)
        # follow_redirects=False；登录态失效通常明确 302 到登录页。
        if response.status_code in (302, 401, 403):
            return Cloud115CookieStatus.EXPIRED
        if response.status_code != 200:
            return Cloud115CookieStatus.UNAVAILABLE
        try:
            data = response.json()
        except Exception:
            return Cloud115CookieStatus.UNAVAILABLE
        if not isinstance(data, dict) or "state" not in data:
            return Cloud115CookieStatus.UNAVAILABLE
        if data["state"] is True:
            return Cloud115CookieStatus.ALIVE
        if data["state"] is False:
            return Cloud115CookieStatus.EXPIRED
        return Cloud115CookieStatus.UNAVAILABLE

    async def check_cookies_alive(self) -> bool:
        """兼容旧调用方的 bool 接口；临时不可用与失效均返回 False。"""
        return await self.probe_cookies_status() is Cloud115CookieStatus.ALIVE

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

    # 秒传/初始化 upload 返回后，115 侧的 pickcode 索引可能还没生效，即时反查会拿
    # 到 Cloud115NotFoundError（data=[]）。这里做与 verify_cloud115_renamed_file
    # 同风格的短退避：首次立即 + 4 次退避（0.3/0.8/1.5/2.5s），总窗口 ~5s；
    # 只对 NotFound 兜底，其他错误立刻透传避免掩盖真问题。
    _PICKCODE_INDEX_WAIT_DELAYS = (0.0, 0.3, 0.8, 1.5, 2.5)

    async def _wait_pickcode_indexed(self, pickcode: str) -> FileMeta:
        last_exc: Cloud115NotFoundError | None = None
        for delay in self._PICKCODE_INDEX_WAIT_DELAYS:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self.pickcode_info(pickcode)
            except Cloud115NotFoundError as exc:
                last_exc = exc
        assert last_exc is not None  # 循环至少执行一次
        raise last_exc

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

    async def mkdir(self, pid: str, name: str) -> str:
        """在 pid 目录下建一个叫 name 的子目录，返回新目录 cid。

        - pid: 父目录 cid，根用 "0"。name: 目录名（不做前后空格清理，上层负责）。
        - 115 允许同目录同名共存 → 上层做 find-or-create 时必须先 list 判存在。
        - 端点：POST webapi.115.com/files/add，body {pid, cname}。
        """
        if not name:
            raise ValueError("name is required")
        if not pid:
            raise ValueError("pid is required (use '0' for root)")
        url = f"{self._BASE_WEBAPI}/files/add"
        payload = await self._request_json(
            "POST", url, data={"pid": pid, "cname": name}
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)
        # 115 成功响应字段名有 category_id / cid / file_id 三种历史写法，兜住任一
        cid = str(
            payload.get("category_id")
            or payload.get("cid")
            or payload.get("file_id")
            or ""
        )
        if not cid:
            raise Cloud115RequestError(
                "mkdir response missing new cid",
                method="POST",
                url=url,
                detail=str(payload)[:200],
            )
        return cid

    # ---- 文件管理（导入管线用：递归枚举 / 复制 / 移动 / 改名 / 删除 / 小文件下载） ----

    async def iter_files_recursive(
        self,
        cid: str,
        *,
        page_size: int = 1000,
    ):
        """递归枚举 cid 目录树下的**全部文件**（不含目录条目），逐条 yield DirEntry。

        - 触发条件：/files 加 show_dir=0 & cur=0（p115client 记载的全树递归模式）。
        - 固定 o=file_name & asc=1 排序，保证大目录跨页分页一致（服务端默认排序不稳定）。
        - 递归模式下每条只带 parent cid（parent_id），**拿不到父目录名** ——
          cid→目录名映射由上层自己用 list_dir 遍历目录结构维护（目录数远小于文件数）。
        - play_long / ic 字段在本响应里白给，导入侧直接消费，无需逐文件再查。
        """
        if not cid:
            raise ValueError("cid is required (use '0' for root)")
        if page_size > self._LIST_DIR_MAX_LIMIT:
            raise ValueError(
                f"page_size {page_size} exceeds server max {self._LIST_DIR_MAX_LIMIT}"
            )
        url = f"{self._BASE_WEBAPI}/files"
        offset = 0
        total = -1
        while total < 0 or offset < total:
            params = {
                "aid": 1,
                "cid": cid,
                "offset": offset,
                "limit": page_size,
                "show_dir": 0,
                "cur": 0,
                "o": "file_name",
                "asc": 1,
            }
            payload = await self._request_json("GET", url, params=params)
            if not payload.get("state"):
                raise self._map_errno(payload, endpoint=url)
            batch = [self._parse_dir_entry(raw) for raw in (payload.get("data") or [])]
            total = int(payload.get("count", 0))
            if not batch:
                break
            for entry in batch:
                yield entry
            offset += len(batch)

    async def copy_files(self, fids: list[str], *, pid: str) -> None:
        """批量复制文件/目录到 pid 目录（云端零流量搬运）。

        - ⚠️ 115 文档明确：copy 勿并发执行、单次 ≤5 万个 → 上层串行分批调用本方法。
        - **复制产生新 fid 和新 pickcode**（仅 sha1 相同）→ 登记必须以复制后
          re-list 目标目录拿到的新条目为准，不能拿源条目的 pickcode 落库。
        - 同账号内复制占双倍空间。
        - 端点：POST webapi.115.com/files/copy，body {pid, fid[0..n]}。
        """
        if not fids:
            raise ValueError("fids is required")
        if not pid:
            raise ValueError("pid is required (use '0' for root)")
        url = f"{self._BASE_WEBAPI}/files/copy"
        data: dict[str, Any] = {"pid": pid}
        for index, fid in enumerate(fids):
            data[f"fid[{index}]"] = fid
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def move_files(self, fids: list[str], *, pid: str) -> None:
        """批量移动文件/目录到 pid 目录。

        - 与 copy 协议同型；**移动保持 fid / pickcode 不变** → 登记可直接用源条目。
        - 不占双倍空间；SDK 保留该底层能力，媒体导入的 cleanup-source 不再使用移动。
        - 端点：POST webapi.115.com/files/move，body {pid, fid[0..n]}。
        """
        if not fids:
            raise ValueError("fids is required")
        if not pid:
            raise ValueError("pid is required (use '0' for root)")
        url = f"{self._BASE_WEBAPI}/files/move"
        data: dict[str, Any] = {"pid": pid}
        for index, fid in enumerate(fids):
            data[f"fid[{index}]"] = fid
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def batch_rename(self, renames: dict[str, str]) -> None:
        """批量改名。renames: {fid: 新名}。

        - ⚠️ 文件新名**必须带扩展名**：115 会把最后一个 '.' 之后的部分截断处理，
          不带扩展名会导致名字被意外截断；且扩展名本身不可改。
        - 改名保持 fid / pickcode 不变。
        - 单批条数上限未见官方文档，上层保守按 30–50/批分批。
        - 端点：POST webapi.115.com/files/batch_rename，body files_new_name[<fid>]=<新名>。
        """
        if not renames:
            raise ValueError("renames is required")
        for fid, new_name in renames.items():
            if not fid or not new_name:
                raise ValueError(f"invalid rename entry: {fid!r} -> {new_name!r}")
        url = f"{self._BASE_WEBAPI}/files/batch_rename"
        data = {f"files_new_name[{fid}]": name for fid, name in renames.items()}
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def rename_file(self, fid: str, new_name: str) -> None:
        """单文件改名；每次请求只提交一个 fid，供需逐项确认的导入流程使用。"""
        if not fid or not new_name:
            raise ValueError(f"invalid rename entry: {fid!r} -> {new_name!r}")
        url = f"{self._BASE_WEBAPI}/files/batch_rename"
        payload = await self._request_json(
            "POST", url, data={f"files_new_name[{fid}]": new_name}
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def delete_files(self, fids: list[str], *, pid: str | None = None) -> None:
        """批量删除文件/目录（进 115 回收站，有误删缓冲）。

        - pid 可选：传删除项所在父目录 cid 可少一次服务端定位（不传也能删）。
        - 端点：POST webapi.115.com/rb/delete，body {fid[0..n], pid?}。
        """
        if not fids:
            raise ValueError("fids is required")
        url = f"{self._BASE_WEBAPI}/rb/delete"
        data: dict[str, Any] = {}
        if pid:
            data["pid"] = pid
        for index, fid in enumerate(fids):
            data[f"fid[{index}]"] = fid
        payload = await self._request_json("POST", url, data=data)
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

    async def download_bytes(
        self,
        pickcode: str,
        *,
        user_agent: str,
        max_bytes: int = 10 * 1024 * 1024,
    ) -> bytes:
        """下载小文件完整内容（字幕等）。封装「拿直链与 GET 必须同 UA」这一易错点。

        - 内部先 get_download_url(pickcode, user_agent) 再用**同一 UA** GET 直链。
        - max_bytes 防御：目标超限直接抛 Cloud115RequestError（本方法只为小文件设计，
          视频请走 /stream 302 或受控 Range 读）。
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        direct = await self.get_download_url(pickcode, user_agent)
        headers = {"User-Agent": user_agent}
        chunks: list[bytes] = []
        received = 0
        try:
            async with self._client.stream("GET", direct.url, headers=headers) as response:
                if response.status_code not in (200, 206):
                    raise Cloud115RequestError(
                        f"http {response.status_code} on GET direct url for pickcode {pickcode}",
                        method="GET",
                        url=direct.url,
                        detail=f"pickcode={pickcode}",
                    )
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise Cloud115RequestError(
                            f"file exceeds max_bytes={max_bytes} for pickcode {pickcode}",
                            method="GET",
                            url=direct.url,
                            detail=f"received>{max_bytes}",
                        )
                    chunks.append(chunk)
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise Cloud115RequestError(
                f"network error downloading pickcode {pickcode}: {exc}",
                method="GET",
                url=direct.url,
                detail=str(exc),
            ) from exc
        return b"".join(chunks)

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
            # HTTP 虽为 POST，但该端点只读取并签发直链，不产生远端文件副作用。
            retryable=True,
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
        """获取视频元数据与 master m3u8 中的清晰度列表（VIP 专属）。"""
        if not pickcode:
            raise ValueError("pickcode is required")
        url = f"{self._BASE_WEBAPI}/files/video"
        payload = await self._request_json(
            "GET",
            url,
            params={"pickcode": pickcode},
        )
        if not payload.get("state"):
            raise self._map_errno(payload, endpoint=url)

        # 先判断转码状态，避免把尚未生成 video_url 的视频误判成非视频文件。
        raw_status = payload.get("file_status")
        if raw_status is not None:
            try:
                file_status = int(raw_status)
            except (TypeError, ValueError):
                file_status = 1
            if file_status != 1:
                raise Cloud115VideoNotReadyError(
                    f"video not ready for pickcode {pickcode} "
                    f"(file_status={file_status})",
                    file_status=file_status,
                    endpoint=url,
                )

        master_m3u8_url = str(payload.get("video_url", "") or "")
        if not master_m3u8_url:
            raise Cloud115NotFoundError(
                f"video_url missing for pickcode {pickcode} (not a video)",
                endpoint=url,
            )

        master_text = await self._get_text(master_m3u8_url)
        definitions = self._parse_master_m3u8(
            master_text,
            base_url=master_m3u8_url,
        )
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
        """获取指定码率或最高码率 variant 的 HLS TS 分段列表。"""
        info = await self.get_video_info(pickcode)
        if not info.definitions:
            raise Cloud115NotFoundError(
                f"no video definitions available for pickcode {pickcode}",
                endpoint=info.master_m3u8_url,
            )
        variant = self._pick_variant(info.definitions, prefer_bandwidth)
        return await self.get_video_segments_for_definition(variant)

    async def get_video_segments_for_definition(
        self,
        definition: VideoDefinition,
    ) -> list[VideoSegment]:
        """读取已解析清晰度分支，避免上层重复请求 master playlist。"""
        if not definition.m3u8_url:
            raise ValueError("definition.m3u8_url is required")
        variant_text = await self._get_text(definition.m3u8_url)
        return self._parse_variant_m3u8(
            variant_text,
            base_url=definition.m3u8_url,
        )

    async def rapid_upload(
        self,
        path: str | Path,
        *,
        pid: str = "0",
    ) -> RapidUploadResult:
        """只尝试秒传一个本地文件，不会回退到普通上传。

        大文件首次初始化可能返回 status=7，要求读取服务端指定范围并再次提交范围哈希；
        最终 status=2 才表示文件已落到目标目录。SDK 根据 UID Cookie 自动识别 Android
        F1 或支付宝小程序 R2 槽，并选择对应的 userkey 接口；其他槽位不支持秒传。
        """
        file_path = Path(path)
        if not file_path.is_file():
            raise ValueError(f"file path is not a regular file: {file_path}")
        if not pid:
            raise ValueError("pid is required")
        upload_protocol = self._rapid_upload_protocol()

        before = self._file_snapshot(file_path)
        size = before[0]
        file_sha1 = (await asyncio.to_thread(self._hash_file, file_path)).upper()
        if self._file_snapshot(file_path) != before:
            return RapidUploadResult(
                status=RapidUploadStatus.FILE_CHANGED,
                path=str(file_path),
                filename=file_path.name,
                size=size,
                sha1=file_sha1,
            )

        response = await self._upload_init(
            filename=file_path.name,
            filesize=size,
            filesha1=file_sha1,
            pid=pid,
            upload_protocol=upload_protocol,
        )
        data = self._upload_data(response)
        status = int(data.get("status") or 0)
        if status == 7:
            sign_key = str(data.get("sign_key") or "")
            sign_check = str(data.get("sign_check") or "")
            if not sign_key or not sign_check:
                raise Cloud115RequestError(
                    "upload init status=7 missing sign_key/sign_check",
                    method="POST",
                    url=f"{self._BASE_UPLOAD}/4.0/initupload.php",
                )
            if self._file_snapshot(file_path) != before:
                return RapidUploadResult(
                    status=RapidUploadStatus.FILE_CHANGED,
                    path=str(file_path),
                    filename=file_path.name,
                    size=size,
                    sha1=file_sha1,
                    raw_response=response,
                )
            range_sha1 = await asyncio.to_thread(
                self._hash_file_range,
                file_path,
                sign_check,
            )
            if self._file_snapshot(file_path) != before:
                return RapidUploadResult(
                    status=RapidUploadStatus.FILE_CHANGED,
                    path=str(file_path),
                    filename=file_path.name,
                    size=size,
                    sha1=file_sha1,
                    raw_response=response,
                )
            response = await self._upload_init(
                filename=file_path.name,
                filesize=size,
                filesha1=file_sha1,
                pid=pid,
                sign_key=sign_key,
                sign_val=range_sha1,
                upload_protocol=upload_protocol,
            )
            data = self._upload_data(response)
            status = int(data.get("status") or 0)

        if status != 2:
            return RapidUploadResult(
                status=RapidUploadStatus.NOT_HIT,
                path=str(file_path),
                filename=file_path.name,
                size=size,
                sha1=file_sha1,
                raw_response=response,
            )
        # initupload 的 status=2 响应里 pickcode 才是真实稳定标识；同响应里
        # 的 fileid 字段是 int 占位符（多为 0，见 SheltonZhu/115driver 里
        # "Useless fields" 注释与 ChenyangGao/p115client 的处理）。业务层需要
        # 真 file_id 走 rename/delete/file_info，只能靠 pickcode 反查。
        pickcode = self._first_text(data, "pick_code", "pickcode")
        if not pickcode:
            raise Cloud115RequestError(
                "upload init status=2 missing pickcode",
                method="POST",
                url=f"{self._BASE_UPLOAD}/4.0/initupload.php",
                detail=str(data)[:200],
            )
        # 新落地文件的索引在 115 侧不是即时的：initupload 刚返回 status=2 就查
        # pickcode 常常撞上 Cloud115NotFoundError（data=[]）。做短退避重试，跟
        # verify_cloud115_renamed_file 一样只兜索引窗口，其它错误立刻透传出去。
        meta = await self._wait_pickcode_indexed(pickcode)
        return RapidUploadResult(
            status=RapidUploadStatus.SUCCESS,
            path=str(file_path),
            filename=file_path.name,
            size=size,
            sha1=file_sha1,
            file_id=meta.file_id,
            pickcode=meta.pickcode or pickcode,
            raw_response=response,
        )

    def _rapid_upload_protocol(self) -> Literal["web", "android"]:
        """从 UID Cookie 的登录槽自动选择秒传所需的 userkey 接口。"""
        uid = self._cookies_dict.get("UID", "")
        match = self._UID_SSOENT_PATTERN.match(uid)
        ssoent = match.group(1) if match else ""
        protocol = self._RAPID_UPLOAD_PROTOCOL_BY_SSOENT.get(ssoent)
        if protocol == "android":
            return "android"
        if protocol == "web":
            return "web"
        raise Cloud115AuthError(
            "rapid upload only supports Android (F1) and Alipay Mini Program (R2) cookies"
        )

    async def _get_upload_userkey(
        self,
        upload_protocol: Literal["web", "android"] = "web",
    ) -> str:
        """懒加载 cookie 上传协议需要的 userkey，只保存在当前客户端实例。"""
        if upload_protocol not in {"web", "android"}:
            raise ValueError("upload_protocol must be 'web' or 'android'")
        if userkey := self._upload_userkeys.get(upload_protocol):
            return userkey
        async with self._upload_userkey_lock:
            if userkey := self._upload_userkeys.get(upload_protocol):
                return userkey
            if upload_protocol == "android":
                url = f"{self._BASE_PROAPI}/android/2.0/user/upload_key"
            else:
                # 网页 Cookie 对 app upload_key 接口会返回 errno=99；网页上传
                # 初始化实际使用的 userkey 由 uploadinfo 接口提供。
                url = f"{self._BASE_PROAPI}/app/uploadinfo"
            payload = await self._request_json("GET", url, retryable=True)
            if not payload.get("state"):
                raise self._map_errno(payload, endpoint=url)
            data = payload.get("data") or {}
            userkey = str(
                payload.get("userkey")
                or payload.get("user_key")
                or (data.get("userkey") if isinstance(data, dict) else "")
                or (data.get("user_key") if isinstance(data, dict) else "")
                or ""
            )
            if not userkey:
                raise Cloud115RequestError(
                    "upload userkey missing from response",
                    method="GET",
                    url=url,
                    detail=str(payload)[:200],
                )
            self._upload_userkeys[upload_protocol] = userkey
            return userkey

    async def _get_upload_app_version(self) -> str:
        """读取官方 Android 当前版本，避免伪造 99.99.99.99 被 WAF 拦截。"""
        if self._upload_app_version:
            return self._upload_app_version
        async with self._upload_app_version_lock:
            if self._upload_app_version:
                return self._upload_app_version
            payload = await self._request_json(
                "GET",
                self._UPLOAD_APP_VERSION_URL,
                # 版本接口是公开接口，不向该域名透传账号 Cookie。
                headers={"Cookie": ""},
                retryable=True,
            )
            data = payload.get("data") or {}
            android = data.get("Android") if isinstance(data, dict) else None
            version = str(android.get("version_code") or "") if isinstance(android, dict) else ""
            if not version:
                raise Cloud115RequestError(
                    "Android upload app version missing from response",
                    method="GET",
                    url=self._UPLOAD_APP_VERSION_URL,
                    detail=str(payload)[:200],
                )
            self._upload_app_version = version
            return version

    async def _upload_init(
        self,
        *,
        filename: str,
        filesize: int,
        filesha1: str,
        pid: str,
        sign_key: str = "",
        sign_val: str = "",
        upload_protocol: Literal["web", "android"] = "web",
    ) -> dict[str, Any]:
        """调用 uplb 初始化接口；这里只提交秒传元数据，不上传文件内容。"""
        url = f"{self._BASE_UPLOAD}/4.0/initupload.php"
        userkey, app_version = await asyncio.gather(
            self._get_upload_userkey(upload_protocol),
            self._get_upload_app_version(),
        )
        payload = {
            "appid": 0,
            "appversion": app_version,
            "fileid": filesha1.upper(),
            "filename": filename,
            "filesize": filesize,
            "target": f"U_1_{pid}",
            "sign_key": sign_key,
            "sign_val": sign_val,
            "topupload": "true",
            "userid": self._user_id,
            "userkey": userkey,
        }
        params, body = make_upload_payload(payload)
        response = await self._request(
            "POST",
            url,
            params=params,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://115.com",
                "Referer": "https://115.com/",
                "User-Agent": (
                    f"Mozilla/5.0 115disk/{app_version} "
                    f"115Browser/{app_version} 115wangpan_android/{app_version}"
                ),
            },
            retryable=False,
        )
        try:
            result = decrypt_upload_response(response.content)
        except Exception as exc:
            raise Cloud115RequestError(
                "invalid encrypted upload init response",
                method="POST",
                url=url,
                detail=str(exc),
            ) from exc
        if not isinstance(result, dict):
            raise Cloud115RequestError(
                "upload init response is not an object",
                method="POST",
                url=url,
            )
        if result.get("state") is False:
            raise self._map_errno(result, endpoint=url)
        return result

    @staticmethod
    def _upload_data(response: dict[str, Any]) -> dict[str, Any]:
        if response.get("state") is False:
            raise Cloud115Error("upload initialization rejected")
        data = response.get("data")
        if not isinstance(data, dict):
            # 旧版 uplb 会直接返回 {status, statuscode, statusmsg}，没有
            # state/data 包装；保留该响应，让上层按 NOT_HIT 处理而不是误判协议崩溃。
            if "status" in response:
                return response
            raise Cloud115RequestError("upload init response missing data")
        return data

    @staticmethod
    def _first_text(data: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    @staticmethod
    def _file_snapshot(path: Path) -> tuple[int, int, int]:
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", 0)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _hash_file_range(path: Path, sign_check: str) -> str:
        try:
            start_text, end_text = sign_check.split("-", 1)
            start, end = int(start_text), int(end_text)
        except ValueError as exc:
            raise Cloud115RequestError(
                f"invalid upload sign_check: {sign_check!r}"
            ) from exc
        if start < 0 or end < start:
            raise Cloud115RequestError(f"invalid upload byte range: {sign_check!r}")
        digest = hashlib.sha1()
        remaining = end - start + 1
        with path.open("rb") as file:
            file.seek(start)
            while remaining:
                chunk = file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise Cloud115RequestError(
                        f"upload byte range exceeds local file: {sign_check!r}"
                    )
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.hexdigest().upper()

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

    # ---- 内部工具 ----

    def _base_headers(self) -> dict[str, str]:
        # Cookie 从内部 dict 动态拼串（保序，便于 acw_tc 等被服务端 Set-Cookie 更新后透传）
        return {
            "Cookie": self.snapshot_cookies(),
            "User-Agent": self._user_agent,
        }

    async def _acquire_request_slot(self) -> None:
        """全局请求限速闸门：保证相邻请求（含重试）间隔 >= _min_request_interval。

        在锁内"预定"下一个发起时刻后立即释放锁再 sleep，避免持锁睡眠阻塞并发协程；
        因此即使多个协程并发调用，也能得到严格匀速的发起节奏。
        """
        if self._min_request_interval <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            start_at = max(now, self._next_request_at)
            self._next_request_at = start_at + self._min_request_interval
            delay = start_at - now
        if delay > 0:
            await asyncio.sleep(delay)

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | bytes | None = None,
        headers: dict[str, str] | None = None,
        retryable: bool | None = None,
    ) -> httpx.Response:
        """带退避重试的 HTTP 请求，返回 2xx httpx.Response。上层再自行 .json() 或 .text。

        - 429：直接抛 Cloud115RateLimitedError（不重试，避免账号信号升级）
        - 5xx / TimeoutException / NetworkError：仅幂等请求最多 2 次退避重试
        - 4xx（401/403）：401/403 → Cloud115AuthError；其它 → Cloud115RequestError

        每次成功响应到达后会 merge Set-Cookie 到内部 cookies dict（保活 acw_tc 等）。
        401/403 抛异常前也会 merge（可能带着新 acw_tc / logout 信号）。
        """
        last_error: Exception | None = None
        should_retry = retryable if retryable is not None else method.upper() in {"GET", "HEAD", "OPTIONS"}
        max_retries = self._MAX_RETRIES if should_retry else 0
        for attempt in range(max_retries + 1):
            # 每次重试前重新拼 headers（因为上一次响应可能 merge 了新的 acw_tc）
            request_headers = self._base_headers()
            if headers:
                request_headers.update(headers)
            try:
                request_kwargs: dict[str, Any] = {
                    "params": params,
                    "headers": request_headers,
                }
                if isinstance(data, bytes):
                    request_kwargs["content"] = data
                else:
                    request_kwargs["data"] = data
                # 限速闸门：紧贴真正的网络发起，重试也各自受限（避免退避后瞬时补偿式突发）
                await self._acquire_request_slot()
                response = await self._client.request(method, url, **request_kwargs)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                last_error = exc
                logger.warning(
                    "cloud115 request transient error method={} url={} attempt={}/{} detail={}",
                    method, url, attempt + 1, max_retries + 1, exc,
                )
                if attempt >= max_retries:
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
            if status == 405:
                # 裸 HTTP 405 来自 webapi 前置的阿里云 WAF（不是 115 应用层 state=false+errno）：
                # 账号/cookie 已被风控冻结。不重试、抛专用异常，让上层立即熔断停批。
                raise Cloud115RiskControlError(
                    f"http 405 (risk control / WAF) on {method} {url}",
                    method=method,
                    url=url,
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
                    method, url, status, attempt + 1, max_retries + 1,
                )
                if attempt >= max_retries:
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
            f"request failed after {max_retries + 1} attempts: {method} {url} ({detail})",
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
        retryable: bool | None = None,
    ) -> dict[str, Any]:
        """_request 的 JSON 便捷封装。2xx 但非 JSON 抛 Cloud115RequestError。"""
        response = await self._request(
            method,
            url,
            params=params,
            data=data,
            headers=headers,
            retryable=retryable,
        )
        try:
            return response.json()
        except Exception as exc:
            raise Cloud115RequestError(
                f"non-json body on {method} {url}",
                method=method,
                url=url,
                detail=str(exc),
            ) from exc

    async def _get_text(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str:
        """GET 文本资源；用于读取 HLS playlist。"""
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
        # play_long / ic 是可选字段：未转码视频、目录、旧响应可能不带，缺省一律 None
        play_long_raw = raw.get("play_long")
        ic_raw = raw.get("ic")
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
            play_long=int(float(play_long_raw)) if play_long_raw not in (None, "") else None,
            ic=int(ic_raw) if ic_raw not in (None, "") else None,
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

    _M3U8_ATTR_PATTERN = re.compile(r'([A-Z0-9-]+)=(?:"([^"]*)"|([^,]*))')

    @classmethod
    def _parse_master_m3u8(
        cls,
        text: str,
        *,
        base_url: str,
    ) -> list[VideoDefinition]:
        """解析 master playlist，并把相对 variant 地址转换为绝对地址。"""
        definitions: list[VideoDefinition] = []
        pending_attrs: dict[str, str] | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXT-X-STREAM-INF:"):
                attrs_text = line[len("#EXT-X-STREAM-INF:") :]
                pending_attrs = {}
                for match in cls._M3U8_ATTR_PATTERN.finditer(attrs_text):
                    value = (
                        match.group(2)
                        if match.group(2) is not None
                        else match.group(3)
                    )
                    pending_attrs[match.group(1)] = value
                continue
            if line.startswith("#"):
                continue

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
    def _parse_variant_m3u8(
        cls,
        text: str,
        *,
        base_url: str,
    ) -> list[VideoSegment]:
        """解析 variant playlist，并把相对 TS 地址转换为绝对地址。"""
        segments: list[VideoSegment] = []
        pending_duration: float | None = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                duration_text = line[len("#EXTINF:") :].split(",", 1)[0]
                try:
                    pending_duration = float(duration_text)
                except ValueError:
                    pending_duration = None
                continue
            if line.startswith("#"):
                continue

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
        """精确匹配偏好码率；未命中时选择最高码率。"""
        if prefer_bandwidth is not None:
            exact = next(
                (
                    definition
                    for definition in definitions
                    if definition.bandwidth == prefer_bandwidth
                ),
                None,
            )
            if exact is not None:
                return exact
        return max(definitions, key=lambda definition: definition.bandwidth)
