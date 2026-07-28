"""Cloud115 兼容客户端门面。

HTTP 会话、请求策略和远端能力已拆分；本类保留原公开方法，避免业务调用方与手动 CLI
在同一次变更中被迫迁移。
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from src.lib.cloud115.capabilities import (
    AuthCapability,
    FilesCapability,
    OfflineCapability,
    PlaybackCapability,
    RapidUploadCapability,
)
from src.lib.cloud115.session import Cloud115Session
from src.lib.cloud115.transport import Cloud115Transport


class Cloud115Client:
    _DEFAULT_UA = Cloud115Session.DEFAULT_USER_AGENT

    def __init__(
        self,
        cookies: str,
        *,
        user_agent: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        min_request_interval: float = 1.0,
        batch_rest_every: int = 0,
        batch_rest_min_seconds: float = 0.0,
        batch_rest_max_seconds: float = 0.0,
        on_pace_wait: Callable[[float], None] | None = None,
    ) -> None:
        self.session = Cloud115Session(cookies, user_agent=user_agent)
        self.transport = Cloud115Transport(
            self.session,
            timeout=timeout,
            http_client=http_client,
            min_request_interval=min_request_interval,
            batch_rest_every=batch_rest_every,
            batch_rest_min_seconds=batch_rest_min_seconds,
            batch_rest_max_seconds=batch_rest_max_seconds,
            on_pace_wait=on_pace_wait,
        )
        # 迁移期保留测试与调试脚本使用的底层 client 只读引用。
        self._client = self.transport._client
        self.auth = AuthCapability(self.transport)
        self.files = FilesCapability(self.transport)
        self.playback = PlaybackCapability(self.transport)
        self.offline = OfflineCapability(self.transport)
        # legacy API 已占用 rapid_upload 方法名，capability 使用复数属性。
        self.rapid_uploads = RapidUploadCapability(self.transport, self.files)

    @staticmethod
    def _parse_cookies(cookies: str) -> dict[str, str]:
        return Cloud115Session.parse_cookies(cookies)

    @staticmethod
    def _parse_user_id_from_dict(cookies_dict: dict[str, str]) -> str:
        return Cloud115Session.parse_user_id_from_dict(cookies_dict)

    @property
    def user_id(self) -> str:
        return self.session.user_id

    @property
    def user_agent(self) -> str:
        return self.session.user_agent

    def snapshot_cookies(self) -> str:
        return self.session.snapshot_cookies()

    def update_cookies(self, cookies: str) -> None:
        self.session.update_cookies(cookies)
        # Cookie 槽位或账号可能已切换，上传协议缓存不能跨会话复用。
        self.rapid_uploads = RapidUploadCapability(self.transport, self.files)

    async def close(self) -> None:
        await self.transport.close()

    async def __aenter__(self) -> "Cloud115Client":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def probe_cookies_status(self, *args, **kwargs):
        return await self.auth.probe_cookies_status(*args, **kwargs)

    async def check_cookies_alive(self, *args, **kwargs):
        return await self.auth.check_cookies_alive(*args, **kwargs)

    async def list_dir(self, *args, **kwargs):
        return await self.files.list_dir(*args, **kwargs)

    async def file_info(self, *args, **kwargs):
        return await self.files.file_info(*args, **kwargs)

    async def pickcode_info(self, *args, **kwargs):
        return await self.files.pickcode_info(*args, **kwargs)

    async def dir_info(self, *args, **kwargs):
        return await self.files.dir_info(*args, **kwargs)

    async def mkdir(self, *args, **kwargs):
        return await self.files.mkdir(*args, **kwargs)

    async def iter_files_recursive(self, *args, **kwargs):
        async for item in self.files.iter_files_recursive(*args, **kwargs):
            yield item

    async def copy_files(self, *args, **kwargs):
        return await self.files.copy_files(*args, **kwargs)

    async def move_files(self, *args, **kwargs):
        return await self.files.move_files(*args, **kwargs)

    async def batch_rename(self, *args, **kwargs):
        return await self.files.batch_rename(*args, **kwargs)

    async def rename_file(self, *args, **kwargs):
        return await self.files.rename_file(*args, **kwargs)

    async def delete_files(self, *args, **kwargs):
        return await self.files.delete_files(*args, **kwargs)

    async def download_bytes(self, *args, **kwargs):
        return await self.playback.download_bytes(*args, **kwargs)

    async def get_download_url(self, *args, **kwargs):
        return await self.playback.get_download_url(*args, **kwargs)

    async def get_video_info(self, *args, **kwargs):
        return await self.playback.get_video_info(*args, **kwargs)

    async def get_video_segments(self, *args, **kwargs):
        return await self.playback.get_video_segments(*args, **kwargs)

    async def get_video_segments_for_definition(self, *args, **kwargs):
        return await self.playback.get_video_segments_for_definition(*args, **kwargs)

    async def rapid_upload(self, *args, **kwargs):
        return await self.rapid_uploads.rapid_upload(*args, **kwargs)

    async def list_offline_tasks(self, *args, **kwargs):
        return await self.offline.list_offline_tasks(*args, **kwargs)

    async def offline_quota(self, *args, **kwargs):
        return await self.offline.offline_quota(*args, **kwargs)

    async def default_download_dir(self, *args, **kwargs):
        return await self.offline.default_download_dir(*args, **kwargs)

    async def add_offline_urls(self, *args, **kwargs):
        return await self.offline.add_offline_urls(*args, **kwargs)

    async def delete_offline_tasks(self, *args, **kwargs):
        return await self.offline.delete_offline_tasks(*args, **kwargs)

    async def clear_offline_tasks(self, *args, **kwargs):
        return await self.offline.clear_offline_tasks(*args, **kwargs)

    async def restart_offline_task(self, *args, **kwargs):
        return await self.offline.restart_offline_task(*args, **kwargs)

    @staticmethod
    def _parse_master_m3u8(text: str, *, base_url: str):
        return PlaybackCapability._parse_master_m3u8(text, base_url=base_url)

    @staticmethod
    def _parse_variant_m3u8(text: str, *, base_url: str):
        return PlaybackCapability._parse_variant_m3u8(text, base_url=base_url)

    @staticmethod
    def _pick_variant(definitions, prefer_bandwidth):
        return PlaybackCapability._pick_variant(definitions, prefer_bandwidth)
