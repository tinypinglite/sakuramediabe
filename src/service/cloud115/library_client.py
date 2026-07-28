"""cloud115 媒体库的 SDK 接入层。

两件事：
1. ``cloud115_client_for(library)``：按库的 backend_config.cookies 建 Cloud115Client 的
   async context manager，退出时把 SDK 内部 merge 过 Set-Cookie 的最新 cookies 快照回写
   到库配置（acw_tc 等 WAF token 30 分钟过期，不回写则重启后首个请求要多一次重种往返）。
2. ``Cloud115KeepaliveService``：APS 周期任务——对所有 cloud115 库探活 + 快照回写，
   cookies 失效时发系统通知引导用户重新扫码。

httpx.AsyncClient 绑定事件循环，API 进程与 APS 线程（各自 asyncio.run）不能共享实例，
所以这里不做全局 client 缓存：每次操作现建、用完关闭 + 回写快照。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.lib.cloud115 import (
    Cloud115AuthError,
    Cloud115DuplicateNameError,
    Cloud115Client,
    Cloud115CookieStatus,
    Cloud115Error,
    Cloud115NotFoundError,
    Cloud115RateLimitedError,
    Cloud115RiskControlError,
    DirMeta,
)
from src.lib.cloud115.session import Cloud115Session
from src.model import MediaLibrary
from src.model.enums import MediaLibraryBackend

# 库管理目录（jav/videos 子树的父级）与离线下载缓冲目录，两者在 115 根目录下平级。
# 下载缓冲目录不属于库子树：离线产物先落这里，完成后经导入管线（cleanup-source）搬进库。
CLOUD115_LIBRARY_ROOT_NAME = "sakuramedia"
CLOUD115_DOWNLOADS_ROOT_NAME = "sakuramedia_downloads"


def require_cloud115_library(library: MediaLibrary) -> dict:
    """校验库是 cloud115 backend 且配置形状完整，返回 backend_config。

    形状由 MediaLibraryService.create_cloud115_library 落库时保证：
    {"cookies": str, "root_cid": str, "app": str}。
    """
    if library.backend != MediaLibraryBackend.CLOUD115.value:
        raise ValueError(
            f"library {library.id} backend is {library.backend!r}, expected cloud115"
        )
    config = library.backend_config or {}
    if not config.get("cookies") or not config.get("root_cid"):
        raise ValueError(f"library {library.id} backend_config missing cookies/root_cid")
    return config


def _persist_cookies_snapshot(
    library_id: int,
    original_config: dict,
    snapshot: str,
) -> bool:
    """以 context 启动配置为 CAS 条件回写 cookies，避免覆盖并发 reauth。"""
    fresh = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
    if fresh is None:
        return False
    current_config = dict(fresh.backend_config or {})
    if current_config != original_config:
        logger.debug(
            "cloud115 cookies snapshot skipped after concurrent config change library_id={}",
            library_id,
        )
        return False
    if current_config.get("cookies") == snapshot:
        return False
    next_config = dict(current_config)
    next_config["cookies"] = snapshot
    updated = (
        MediaLibrary.update(backend_config=next_config)
        .where(
            MediaLibrary.id == library_id,
            MediaLibrary.backend_config == current_config,
        )
        .execute()
    )
    if updated:
        logger.debug("cloud115 cookies snapshot persisted library_id={}", library_id)
        return True
    logger.debug(
        "cloud115 cookies snapshot CAS lost library_id={}",
        library_id,
    )
    return False


@asynccontextmanager
async def cloud115_client_for(
    library: MediaLibrary,
    *,
    user_agent: str | None = None,
    min_request_interval: float = 1.0,
    batch_pacing: bool = False,
    on_pace_wait=None,
) -> AsyncIterator[Cloud115Client]:
    """按库配置建 Cloud115Client；可指定请求 UA，退出时回写 cookies 快照。

    默认把同一客户端内的请求按 1 秒间隔匀速化，规避 webapi 前置 WAF 的风控阈值；
    高频写入场景可按需覆盖该间隔。

    ``batch_pacing``：批量链路（导入 / 秒传 / 巡检）显式开启后，每打满
    ``cloud115_batch_rest_every_requests`` 个 webapi 请求会额外长休一次。
    交互路径（播放取直链、GUI 浏览目录、离线提交）**必须保持默认关闭**——
    用户正在等待的请求撞上十几秒强制休息就是事故。
    """
    config = require_cloud115_library(library)
    original_config = dict(config)
    original_cookies = original_config["cookies"]
    downloads = settings.downloads
    client = Cloud115Client(
        cookies=original_cookies,
        user_agent=user_agent,
        min_request_interval=min_request_interval,
        batch_rest_every=(
            downloads.cloud115_batch_rest_every_requests if batch_pacing else 0
        ),
        batch_rest_min_seconds=downloads.cloud115_batch_rest_min_seconds,
        batch_rest_max_seconds=downloads.cloud115_batch_rest_max_seconds,
        on_pace_wait=on_pace_wait,
    )
    try:
        yield client
    finally:
        snapshot = client.snapshot_cookies()
        await client.close()
        if snapshot != original_cookies:
            _persist_cookies_snapshot(library.id, original_config, snapshot)


def map_cloud115_error(exc: Cloud115Error) -> ApiError:
    """SDK 异常 → API 错误的统一映射（浏览 / 导入触发等同步接口共用）。

    AuthError 单独给 cloud115_cookies_invalid：前端据此引导用户重新扫码，
    与其它 5xx 类上游故障区分开。
    """
    if isinstance(exc, Cloud115AuthError):
        return ApiError(
            422, "cloud115_cookies_invalid",
            "115 cookies 已失效，请重新扫码登录",
            {"detail": str(exc)},
        )
    if isinstance(exc, Cloud115NotFoundError):
        return ApiError(
            404, "cloud115_dir_not_found",
            "115 目录不存在或已删除",
            {"detail": str(exc)},
        )
    if isinstance(exc, Cloud115DuplicateNameError):
        # 正常情况下 find_or_create_subdir 会自愈掉重名竞态，冒到这里说明是
        # "115 说重名、重扫又找不到" 的自相矛盾状态，或调用方直接裸 mkdir 撞名。
        return ApiError(
            409, "cloud115_duplicate_name",
            "115 上已存在同名目录",
            {"detail": str(exc)},
        )
    if isinstance(exc, Cloud115RateLimitedError):
        return ApiError(
            429, "cloud115_rate_limited",
            "115 正在限流，请稍后再试",
            {"detail": str(exc)},
        )
    if isinstance(exc, Cloud115RiskControlError):
        # 触发 115 风控（WAF 405）：账号被临时冻结，语义接近限流，明确回 429 引导稍后重试。
        return ApiError(
            429, "cloud115_risk_control",
            "115 触发风控（账号被临时限制），请稍后再试",
            {"detail": str(exc)},
        )
    return ApiError(
        502, "cloud115_upstream_error",
        "115 上游调用失败",
        {"detail": str(exc)},
    )


async def lookup_subdir_cid(
    client: Cloud115Client, *, parent_cid: str, name: str
) -> str | None:
    """分页找 parent_cid 下叫 name 的子目录，返回 cid；不存在返回 None。

    同名目录只取首个命中（115 的其它写入路径——转存、云下载、上传——确实可能造成
    同名并存，只有 ``files/add`` 会拒绝重名）。
    """
    offset = 0
    while True:
        entries, total = await client.list_dir(parent_cid, offset=offset, limit=1150)
        for entry in entries:
            if entry.is_dir and entry.name == name:
                return entry.entry_id
        offset += len(entries)
        if not entries or offset >= total:
            return None


async def find_or_create_subdir(
    client: Cloud115Client, *, parent_cid: str, name: str
) -> str:
    """在 parent_cid 下找叫 name 的子目录，没有就建，返回 cid。**竞态安全**。

    ``files/add`` 对重名目录返回 errno=20004（HTTP 200 + state=false，2026-07-29 实测），
    既不幂等也不建重名目录。于是"扫描→未命中→mkdir"之间的窗口里若有并发作业抢先建好，
    我们的 mkdir 会被拒——此时重扫一遍取对方建好的 cid 即可收敛，两边都拿到同一个目录。

    这条自愈路径是必需的：曾经的前提"调用方按库互斥即可保证串行"在 d70a532 移除库级
    mutex 后已经不成立（``mutex_key=None``），而 API 手动触发与 APS 定时任务本来也无法
    靠单个 mutex 串起来。
    """
    cid = await lookup_subdir_cid(client, parent_cid=parent_cid, name=name)
    if cid is not None:
        return cid
    try:
        return await client.mkdir(parent_cid, name)
    except Cloud115DuplicateNameError:
        logger.info(
            "cloud115 subdir created concurrently, reusing it parent_cid={} name={}",
            parent_cid, name,
        )
        cid = await lookup_subdir_cid(client, parent_cid=parent_cid, name=name)
        if cid is None:
            # 115 说重名、重扫又找不到：状态自相矛盾，不静默吞，交给上层暴露。
            raise
        return cid


async def ensure_download_root_cid(
    library: MediaLibrary, client: Cloud115Client
) -> str:
    """取库的离线下载缓冲目录 cid；存量库缺失时 find-or-create 并回写 backend_config。

    建库时（create_cloud115_library）已同步创建；本函数兜住升级前创建的存量库。
    回写只在 download_root_cid 仍缺失时进行，避免覆盖并发 reauth 更新的 cookies。
    """
    config = dict(library.backend_config or {})
    existing = config.get("download_root_cid")
    if existing:
        return existing
    download_root_cid = await find_or_create_subdir(
        client, parent_cid="0", name=CLOUD115_DOWNLOADS_ROOT_NAME
    )
    # 重新读最新配置合并写回：只补 download_root_cid 一个键，不动 cookies 等其它字段。
    fresh = MediaLibrary.get_or_none(MediaLibrary.id == library.id)
    if fresh is not None:
        next_config = dict(fresh.backend_config or {})
        if not next_config.get("download_root_cid"):
            next_config["download_root_cid"] = download_root_cid
            MediaLibrary.update(backend_config=next_config).where(
                MediaLibrary.id == library.id
            ).execute()
            # 同步内存对象，调用方继续用 library.backend_config 时不落伍。
            library.backend_config = next_config
    return download_root_cid


async def assert_cid_outside_library_root(
    client: Cloud115Client, *, source_cid: str, root_cid: str
) -> DirMeta:
    """校验 source_cid 与库管理目录（root_cid 子树）互不包含。前端已禁选，服务端兜底。

    三种拒绝情形：
    1. source == root：直接选中管理目录；
    2. source 在 root 子树内：面包屑链含 root_cid（cleanup/move 会动库内文件）；
    3. root 在 source 子树内（含 source 为根目录 "0"）：递归枚举会把库存量当导入源，
       copy 模式意味着库内容自我复制翻倍，move 模式会把库结构搬走。
    """
    if source_cid == root_cid:
        raise ApiError(
            422, "cloud115_source_inside_library",
            "导入源不能是媒体库管理目录",
            {"source_cid": source_cid, "root_cid": root_cid},
        )
    source_meta = await client.dir_info(source_cid)
    if any(crumb.file_id == root_cid for crumb in source_meta.paths):
        raise ApiError(
            422, "cloud115_source_inside_library",
            "导入源不能位于媒体库管理目录内部",
            {"source_cid": source_cid, "root_cid": root_cid},
        )
    root_meta = await client.dir_info(root_cid)
    if any(crumb.file_id == source_cid for crumb in root_meta.paths):
        raise ApiError(
            422, "cloud115_source_contains_library",
            "导入源不能包含媒体库管理目录（请选择具体的来源子目录）",
            {"source_cid": source_cid, "root_cid": root_cid},
        )
    # 调用方通常还需要源目录名称和面包屑，直接复用本次查询结果。
    return source_meta


class Cloud115KeepaliveService:
    """cloud115 cookies 保活：周期裁剪 + 探活 + 快照回写 + 失效通知。

    acw_tc（阿里云 WAF token）30 分钟过期，SDK 会在响应里自动 merge 服务端刷新值；
    本任务的意义是把刷新值持久化，并及早发现 UID/CID/SEID 长效凭据失效。

    同时承担 cookie 体积的兜底修复：SDK 侧已按 ESSENTIAL_COOKIE_KEYS 白名单收
    Set-Cookie，但扫码登录等直接写 backend_config 的路径不经过 SDK，历史库里也可能
    残留已积累的 WAF 挑战 cookie，所以这里每轮显式裁一次。
    """

    @staticmethod
    def _cloud115_libraries() -> list[MediaLibrary]:
        return list(
            MediaLibrary.select().where(
                MediaLibrary.backend == MediaLibraryBackend.CLOUD115.value
            )
        )

    @staticmethod
    def prune_library_cookies(library: MediaLibrary) -> int:
        """裁掉该库已落库 cookie 里的非必需项，返回丢弃条数。

        必须在探活**之前**执行：cookie 一旦涨过 nginx 的 8KB 请求头上限，连探活自己
        都会被回 400，放在探活之后就永远等不到修复时机。
        """
        fresh = MediaLibrary.get_or_none(MediaLibrary.id == library.id)
        if fresh is None:
            return 0
        original_config = dict(fresh.backend_config or {})
        current = original_config.get("cookies") or ""
        if not current:
            return 0
        pruned, dropped = Cloud115Session.prune_cookies(current)
        # 裁完必须仍是可用会话；缺 UID 说明这份 cookie 本就不完整，交给探活报失效。
        if dropped <= 0 or "UID=" not in pruned:
            return 0
        if not _persist_cookies_snapshot(library.id, original_config, pruned):
            return 0
        # 同步内存对象，紧接着的探活直接用裁剪后的 cookie。
        next_config = dict(original_config)
        next_config["cookies"] = pruned
        library.backend_config = next_config
        logger.info(
            "cloud115 cookies pruned library_id={} name={} dropped={} bytes={}->{}",
            library.id, library.name, dropped, len(current), len(pruned),
        )
        return dropped

    @classmethod
    async def probe_library_cookies_status(
        cls,
        library: MediaLibrary,
    ) -> Cloud115CookieStatus:
        """探测单个 cloud115 库，并把异常归一为稳定的三态结果。"""
        try:
            async with cloud115_client_for(library) as client:
                return await client.probe_cookies_status()
        except Cloud115AuthError:
            return Cloud115CookieStatus.EXPIRED
        except Exception as exc:
            logger.warning(
                "cloud115 cookies probe raised library_id={} name={} detail={}",
                library.id, library.name, exc,
            )
            return Cloud115CookieStatus.UNAVAILABLE

    @classmethod
    async def _check_library(cls, library: MediaLibrary) -> Cloud115CookieStatus:
        """兼容既有调用和测试替换点；新调用方使用公开探测方法。"""
        return await cls.probe_library_cookies_status(library)

    @classmethod
    def run(cls, progress_callback=None) -> dict:
        libraries = cls._cloud115_libraries()
        stats = {
            "total": len(libraries),
            "alive": 0,
            "expired": 0,
            "unavailable": 0,
            "pruned_cookies": 0,
        }
        for library in libraries:
            # 先裁后探：裁剪失败不影响探活，只是这轮没能修复体积。
            try:
                stats["pruned_cookies"] += cls.prune_library_cookies(library)
            except Exception as exc:
                logger.warning(
                    "cloud115 cookies prune failed library_id={} name={} detail={}",
                    library.id, library.name, exc,
                )
            try:
                cookie_status = asyncio.run(cls._check_library(library))
            except Cloud115AuthError:
                cookie_status = Cloud115CookieStatus.EXPIRED
            except Exception as exc:
                cookie_status = Cloud115CookieStatus.UNAVAILABLE
                logger.warning(
                    "cloud115 cookies probe raised library_id={} name={} detail={}",
                    library.id, library.name, exc,
                )
            if cookie_status is Cloud115CookieStatus.ALIVE:
                stats["alive"] += 1
                continue
            if cookie_status is Cloud115CookieStatus.UNAVAILABLE:
                stats["unavailable"] += 1
                logger.warning(
                    "cloud115 cookies probe unavailable library_id={} name={}",
                    library.id, library.name,
                )
                continue
            stats["expired"] += 1
            logger.warning(
                "cloud115 cookies expired library_id={} name={}", library.id, library.name
            )
            # 通知有未读去重，周期任务不会刷屏。
            from src.service.cloud115.notifications import (
                create_cloud115_cookies_expired_notification,
            )

            create_cloud115_cookies_expired_notification(
                library_name=library.name,
                library_id=library.id,
            )
        return stats
