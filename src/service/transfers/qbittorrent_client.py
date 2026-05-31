import re
import time
from typing import List, Optional

import httpx
import libtorrent as lt
import qbittorrentapi
from loguru import logger

from src.model import DownloadClient
from src.service.transfers.common import CLIENT_QB_TAG_PREFIX, SYSTEM_QB_TAG


class QBittorrentClientError(Exception):
    pass


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

    def list_torrents(self, *, client_id: Optional[int] = None) -> List[dict]:
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

    def get_torrent(self, info_hash: str, *, allow_missing: bool = False) -> Optional[dict]:
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

    def _download_torrent_file(self, torrent_url: str) -> bytes:
        try:
            response = self.http_client.get(torrent_url)
            response.raise_for_status()
            return response.content
        except Exception as exc:
            raise QBittorrentClientError(str(exc)) from exc

    def _login(self) -> None:
        try:
            self.client.auth_log_in()
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

    def _submit_torrent(self, *, info_hash: str, tags: List[str], **add_kwargs) -> None:
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
    def _to_dict(torrent) -> dict:
        content_path = getattr(torrent, "content_path", None) or getattr(torrent, "save_path", "")
        return {
            "info_hash": (getattr(torrent, "hash", "") or "").lower(),
            "name": getattr(torrent, "name", ""),
            "progress": float(getattr(torrent, "progress", 0.0) or 0.0),
            "state": getattr(torrent, "state", ""),
            "save_path": str(content_path),
            "tags": getattr(torrent, "tags", "") or "",
        }
