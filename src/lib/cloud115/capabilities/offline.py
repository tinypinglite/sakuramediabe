from __future__ import annotations

from typing import Any, Literal

from src.lib.cloud115.capabilities.base import Cloud115Capability
from src.lib.cloud115.exceptions import Cloud115NotFoundError
from src.lib.cloud115.types import (
    DirEntry,
    OfflineQuota,
    OfflineTask,
    OfflineTaskAddResult,
    OfflineTaskPage,
)

_CLEAR_SCOPE_TO_FLAG = {
    "finished": 0, "all": 1, "failed": 2, "running": 3,
    "finished_with_source": 4, "all_with_source": 5,
}
ClearScope = Literal[
    "finished", "all", "failed", "running",
    "finished_with_source", "all_with_source",
]


class OfflineCapability(Cloud115Capability):
    _LIXIAN_URL = "https://115.com/web/lixian/"
    _OFFLINE_SPACE_URL = "https://115.com/?ct=lixian&ac=space"
    _OFFLINE_DOWNPATH_URL = "https://115.com/?ct=lixian&ac=get_id"

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
