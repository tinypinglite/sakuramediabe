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
        if magnet_url:
            info_hash = self.parse_hash_from_magnet(magnet_url)
            response = self.client.torrents_add(
                urls=magnet_url,
                rename=rename,
                save_path=save_path,
                tags=",".join(tags),
            )
            self._ensure_add_success(response)
        elif torrent_url:
            torrent_bytes = self._download_torrent_file(torrent_url)
            info_hash = self.parse_hash_from_torrent(torrent_bytes)
            response = self.client.torrents_add(
                torrent_files=torrent_bytes,
                rename=rename,
                save_path=save_path,
                tags=",".join(tags),
            )
            self._ensure_add_success(response)
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

    @staticmethod
    def _ensure_add_success(response) -> None:
        if response != "Ok.":
            raise QBittorrentClientError(f"unexpected qBittorrent add response: {response}")

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
