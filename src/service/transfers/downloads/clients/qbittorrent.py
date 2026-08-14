import re
import time

import httpx
import libtorrent as lt
import qbittorrentapi

from src.model import DownloadClient
from src.service.transfers.downloads.common import (
    CLIENT_QB_TAG_PREFIX,
    SYSTEM_QB_TAG,
    is_qb_managed_torrent,
)


class QBittorrentClientError(Exception):
    pass


class QBittorrentTorrentNotManagedError(QBittorrentClientError):
    """目标种子不属于 SakuraMedia 当前下载客户端。"""


class QBittorrentTorrentNotFoundError(QBittorrentClientError):
    """目标种子已不在 qBittorrent 中。"""


class QBittorrentClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        client=None,
        http_client: httpx.Client | None = None,
    ):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.client = client or qbittorrentapi.Client(
            host=base_url,
            username=username,
            password=password,
        )
        self.http_client = http_client or httpx.Client(timeout=120.0, follow_redirects=True, trust_env=False)
        self._logged_in = False

    @classmethod
    def from_download_client(cls, download_client: DownloadClient) -> "QBittorrentClient":
        return cls(
            base_url=download_client.base_url,
            username=download_client.username,
            password=download_client.password,
        )

    def add_candidate(
        self,
        *,
        magnet_url: str,
        torrent_url: str,
        save_path: str,
        rename: str,
        client_id: int,
    ) -> dict:
        tags = [SYSTEM_QB_TAG, f"{CLIENT_QB_TAG_PREFIX}{client_id}"]
        self._login()
        # 按内容而非字段名判定来源：磁力链可能被索引器放进 torrent_url 字段，
        # 若仅凭字段名处理会把磁力链当成 .torrent 文件去 HTTP 下载，导致 "unsupported protocol 'magnet://'"。
        magnet_link = ""
        torrent_file_url = ""
        for link in (magnet_url, torrent_url):
            link = (link or "").strip()
            if not link:
                continue
            if self._is_magnet(link):
                magnet_link = magnet_link or self._normalize_magnet(link)
            elif not torrent_file_url:
                torrent_file_url = link

        if magnet_link:
            info_hash = self.parse_hash_from_magnet(magnet_link)
            self._submit_torrent(
                info_hash=info_hash,
                tags=tags,
                urls=magnet_link,
                rename=rename,
                save_path=save_path,
            )
        elif torrent_file_url:
            torrent_bytes = self._download_torrent_file(torrent_file_url)
            info_hash = self.parse_hash_from_torrent(torrent_bytes)
            self._submit_torrent(
                info_hash=info_hash,
                tags=tags,
                torrent_files=torrent_bytes,
                rename=rename,
                save_path=save_path,
            )
        else:
            raise QBittorrentClientError("candidate missing magnet_url and torrent_url")

        torrent = self.get_torrent(info_hash, allow_missing=True)
        if torrent is None:
            return {
                "info_hash": info_hash,
                "name": rename,
                "progress": 0.0,
                "state": "queuedDL",
                "save_path": save_path,
            }
        return torrent

    def list_torrents(self, *, client_id: int | None = None) -> list[dict]:
        self._login()
        try:
            torrents = list(self.client.torrents_info(tag=SYSTEM_QB_TAG))
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

        if client_id is None:
            return [self._to_dict(item) for item in torrents]

        client_tag = f"{CLIENT_QB_TAG_PREFIX}{client_id}"
        result = []
        for item in torrents:
            tags = (getattr(item, "tags", "") or "").split(",")
            normalized_tags = [tag.strip() for tag in tags if tag.strip()]
            if client_tag in normalized_tags:
                result.append(self._to_dict(item))
        return result

    def inspect_status(self) -> dict:
        """只读探测 qBittorrent Web API 可用性，返回基础版本信息。"""
        self._login()
        try:
            version = self.client.app_version()
            web_api_version = self.client.app_web_api_version()
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc
        return {
            "version": str(version) if version is not None else None,
            "web_api_version": str(web_api_version) if web_api_version is not None else None,
        }

    def list_directory_names(self, directory_path: str) -> list[str]:
        """读取 qBittorrent 视角下的目录内容，并归一化为条目名称列表。"""
        self._login()
        try:
            entries = self.client.app_get_directory_content(directory_path)
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc
        names = []
        for item in entries:
            name = self._directory_entry_name(item)
            if name:
                names.append(name)
        return names

    def list_torrent_files(self, info_hash: str) -> list[dict]:
        # 取单个种子的文件列表（不含"是否受管"校验，供内部清理任务使用）。
        self._login()
        return self._fetch_torrent_files(info_hash)

    def list_managed_torrent_files(self, info_hash: str, *, client_id: int) -> list[dict]:
        # 面向 UI 的文件列表：先确认种子存在且属于当前下载客户端，再取文件清单，
        # 与 pause/resume/delete 走同一套存在性 + 标签校验语义，避免把别人的种子文件暴露出来。
        self._login()
        torrent = self.get_torrent(info_hash, allow_missing=True)
        if torrent is None:
            raise QBittorrentTorrentNotFoundError("torrent not found")
        self._ensure_managed_torrent(torrent, client_id)
        return self._fetch_torrent_files(info_hash)

    def _fetch_torrent_files(self, info_hash: str) -> list[dict]:
        # 归一化成业务侧需要的字段，屏蔽 qbittorrentapi 的对象结构。
        try:
            files = self.client.torrents_files(torrent_hash=info_hash)
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc
        result = []
        for position, item in enumerate(files):
            # qB 4.4.0(Web API 2.8.2)起文件列表才返回 index；旧版本缺该字段时回退用列表位置，
            # 避免所有文件 index 都退化成 0，进而 set_files_not_download / rename 误操作错误文件。
            raw_index = getattr(item, "index", None)
            index = int(raw_index) if raw_index is not None else position
            result.append(
                {
                    "index": index,
                    "name": getattr(item, "name", "") or "",
                    "size": int(getattr(item, "size", 0) or 0),
                    "priority": int(getattr(item, "priority", 0) or 0),
                }
            )
        return result

    def set_files_not_download(self, info_hash: str, file_indexes: list[int]) -> None:
        # 把指定文件优先级设为 0（不下载），让 qBittorrent 跳过这些文件。
        if not file_indexes:
            return
        self._login()
        try:
            self.client.torrents_file_priority(
                torrent_hash=info_hash,
                file_ids=file_indexes,
                priority=0,
            )
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

    def rename_torrent_file(self, info_hash: str, file_index: int, new_name: str) -> None:
        # 重命名种子内文件，仅用于给待删除小文件打标记；qBittorrent 重命名不会删除任何已下载数据，
        # 真正回收空间靠调用方后续的物理删除（priority=0 同样不会删除已落盘的字节）。
        self._login()
        try:
            self.client.torrents_rename_file(
                torrent_hash=info_hash,
                file_id=file_index,
                new_file_name=new_name,
            )
        except qbittorrentapi.Conflict409Error:
            # 文件已被重命名/移除（重复处理）时按幂等成功处理。
            return
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

    def get_torrent(self, info_hash: str, *, allow_missing: bool = False) -> dict | None:
        self._login()
        for _ in range(5):
            try:
                items = list(self.client.torrents_info(torrent_hashes=[info_hash]))
            except Exception as exc:
                raise QBittorrentClientError(str(exc)) from exc
            if items:
                return self._to_dict(items[0])
            time.sleep(0.2)
        if allow_missing:
            return None
        raise QBittorrentClientError(f"torrent not found: {info_hash}")

    def get_torrent_progress_delta(self) -> dict:
        """读取 qB Sync 增量数据，供长连接复用同一个 RID 会话。"""
        self._login()
        try:
            payload = self.client.sync.maindata.delta()
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

        torrents = {}
        for info_hash, item in (payload.get("torrents") or {}).items():
            torrents[str(info_hash).lower()] = self._to_progress_dict(item)
        return {
            "full_update": bool(payload.get("full_update")),
            "torrents": torrents,
            "torrents_removed": [str(item).lower() for item in (payload.get("torrents_removed") or [])],
            "server_state": self._to_progress_dict(payload.get("server_state") or {}),
        }

    def pause_torrent(self, info_hash: str, *, client_id: int) -> None:
        self._operate_managed_torrent(
            info_hash,
            client_id=client_id,
            operation=lambda: self.client.torrents_stop(torrent_hashes=info_hash),
        )

    def resume_torrent(self, info_hash: str, *, client_id: int) -> None:
        self._operate_managed_torrent(
            info_hash,
            client_id=client_id,
            operation=lambda: self.client.torrents_start(torrent_hashes=info_hash),
        )

    def delete_torrent(self, info_hash: str, *, client_id: int, delete_files: bool) -> bool:
        """删除受管种子；远端已不存在时返回 False，供本地清理陈旧任务。"""
        self._login()
        torrent = self.get_torrent(info_hash, allow_missing=True)
        if torrent is None:
            return False
        self._ensure_managed_torrent(torrent, client_id)
        try:
            self.client.torrents_delete(
                torrent_hashes=info_hash,
                delete_files=delete_files,
            )
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc
        return True

    def _operate_managed_torrent(self, info_hash: str, *, client_id: int, operation) -> None:
        self._login()
        torrent = self.get_torrent(info_hash, allow_missing=True)
        if torrent is None:
            raise QBittorrentTorrentNotFoundError("torrent not found")
        self._ensure_managed_torrent(torrent, client_id)
        try:
            operation()
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

    @staticmethod
    def _ensure_managed_torrent(torrent: dict, client_id: int) -> None:
        if not is_qb_managed_torrent(torrent.get("tags"), client_id):
            raise QBittorrentTorrentNotManagedError("torrent is not managed by this download client")

    def _download_torrent_file(self, torrent_url: str) -> bytes:
        try:
            response = self.http_client.get(torrent_url)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

    def _login(self) -> None:
        if self._logged_in:
            return
        try:
            self.client.auth_log_in()
            # qBittorrent Web API 客户端会在 Cookie 失效时自行重新认证；这里仅避免同一长连接每秒重复登录。
            self._logged_in = True
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

    @staticmethod
    def _is_magnet(value: str) -> bool:
        return (value or "").strip().lower().startswith("magnet:")

    @staticmethod
    def _normalize_magnet(value: str) -> str:
        # 规范化磁力链：部分索引器返回非标准的 "magnet://"，统一改写成标准的 "magnet:?"
        text = (value or "").strip()
        if text[:9].lower() == "magnet://":
            rest = text[9:]
            text = "magnet:" + rest if rest.startswith("?") else "magnet:?" + rest
        return text

    @staticmethod
    def parse_hash_from_magnet(magnet_url: str) -> str:
        matched = re.search(r"urn:btih:([A-Za-z0-9]+)", magnet_url)
        if not matched:
            raise QBittorrentClientError("invalid magnet url")
        return matched.group(1).lower()

    @staticmethod
    def parse_hash_from_torrent(torrent_bytes: bytes) -> str:
        try:
            return str(lt.torrent_info(torrent_bytes).info_hash()).lower()
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

    def _submit_torrent(self, *, info_hash: str, tags: list[str], **add_kwargs) -> None:
        tag_string = ",".join(tags)
        try:
            response = self.client.torrents_add(tags=tag_string, **add_kwargs)
        except qbittorrentapi.Conflict409Error:
            # 种子已存在于 qBittorrent（重复提交）：补打系统标签后按幂等成功处理，复用已存在的种子
            self.client.torrents_add_tags(tags=tag_string, torrent_hashes=info_hash)
            return
        self._ensure_add_success(response)

    @staticmethod
    def _ensure_add_success(response) -> None:
        # qBittorrent 4.x：/torrents/add 成功返回文本 "Ok."，失败返回 "Fails."
        if isinstance(response, str):
            if response.strip() != "Ok.":
                raise QBittorrentClientError(f"unexpected qBittorrent add response: {response}")
            return
        # qBittorrent 5.x：返回 JSON 元数据(TorrentsAddedMetadata)，按 failure_count 判定失败
        if response.get("failure_count", 0):
            raise QBittorrentClientError(f"qBittorrent add failed: {response}")

    @staticmethod
    def _directory_entry_name(entry) -> str:
        if isinstance(entry, str):
            raw_name = entry
        elif isinstance(entry, dict):
            raw_name = entry.get("name") or entry.get("path") or ""
        else:
            raw_name = getattr(entry, "name", None) or getattr(entry, "path", "")
        text = str(raw_name or "").strip()
        if not text:
            return ""
        return text.replace("\\", "/").rstrip("/").split("/")[-1]

    @staticmethod
    def _to_dict(torrent) -> dict:
        content_path = getattr(torrent, "content_path", None) or getattr(torrent, "save_path", "")
        return {
            "info_hash": (getattr(torrent, "hash", "") or "").lower(),
            "name": getattr(torrent, "name", ""),
            "progress": float(getattr(torrent, "progress", 0.0) or 0.0),
            "state": getattr(torrent, "state", ""),
            "save_path": str(content_path),
            "tags": getattr(torrent, "tags", "") or "",
            # qB 官方定义："Last time (Unix Epoch) when a chunk was downloaded/uploaded"。
            # 死种判定的唯一依据；从未活动的种子 qB 返回的是添加时刻，不是哨兵值。
            "last_activity": getattr(torrent, "last_activity", None),
        }

    @staticmethod
    def _to_progress_dict(item) -> dict:
        """兼容 qbittorrent-api 的字典和属性对象，保留 Sync 原始字段。"""
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "items"):
            return dict(item.items())
        return {
            key: getattr(item, key)
            for key in (
                "name",
                "tags",
                "progress",
                "state",
                "dlspeed",
                "upspeed",
                "downloaded",
                "size",
                "eta",
                "last_activity",
                "dl_info_speed",
                "up_info_speed",
                "connection_status",
            )
            if hasattr(item, key)
        }
