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
from src.lib.cloud115 import (
    Cloud115AuthError,
    Cloud115Client,
    Cloud115CookieStatus,
    Cloud115Error,
    Cloud115NotFoundError,
    Cloud115RateLimitedError,
)
from src.model import MediaLibrary
from src.model.enums import MediaLibraryBackend


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
async def cloud115_client_for(library: MediaLibrary) -> AsyncIterator[Cloud115Client]:
    """按库配置建 Cloud115Client；退出时回写 cookies 快照（有变化才写库）。"""
    config = require_cloud115_library(library)
    original_config = dict(config)
    original_cookies = original_config["cookies"]
    client = Cloud115Client(cookies=original_cookies)
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
    if isinstance(exc, Cloud115RateLimitedError):
        return ApiError(
            429, "cloud115_rate_limited",
            "115 正在限流，请稍后再试",
            {"detail": str(exc)},
        )
    return ApiError(
        502, "cloud115_upstream_error",
        "115 上游调用失败",
        {"detail": str(exc)},
    )


async def find_or_create_subdir(
    client: Cloud115Client, *, parent_cid: str, name: str
) -> str:
    """在 parent_cid 下找叫 name 的子目录，没有就建，返回 cid。

    115 允许同名目录并存 → 必须先分页 list 判存在再建；调用方需保证同一 parent
    下的 find-or-create 串行（导入任务按库互斥即可满足），避免并发双建。
    """
    offset = 0
    while True:
        entries, total = await client.list_dir(parent_cid, offset=offset, limit=1150)
        for entry in entries:
            if entry.is_dir and entry.name == name:
                return entry.entry_id
        offset += len(entries)
        if not entries or offset >= total:
            break
    return await client.mkdir(parent_cid, name)


async def assert_cid_outside_library_root(
    client: Cloud115Client, *, source_cid: str, root_cid: str
) -> None:
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


class Cloud115KeepaliveService:
    """cloud115 cookies 保活：周期探活 + 快照回写 + 失效通知。

    acw_tc（阿里云 WAF token）30 分钟过期，SDK 会在响应里自动 merge 服务端刷新值；
    本任务的意义是把刷新值持久化，并及早发现 UID/CID/SEID 长效凭据失效。
    """

    @staticmethod
    def _cloud115_libraries() -> list[MediaLibrary]:
        return list(
            MediaLibrary.select().where(
                MediaLibrary.backend == MediaLibraryBackend.CLOUD115.value
            )
        )

    @classmethod
    async def _check_library(cls, library: MediaLibrary) -> Cloud115CookieStatus:
        async with cloud115_client_for(library) as client:
            return await client.probe_cookies_status()

    @classmethod
    def run(cls, progress_callback=None) -> dict:
        libraries = cls._cloud115_libraries()
        stats = {"total": len(libraries), "alive": 0, "expired": 0, "unavailable": 0}
        for library in libraries:
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
            # 通知有未读去重（见 ActivityService），周期任务不会刷屏
            from src.service.system import ActivityService

            ActivityService.create_cloud115_cookies_expired_notification(
                library_name=library.name,
                library_id=library.id,
            )
        return stats
