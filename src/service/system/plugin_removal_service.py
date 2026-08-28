"""Safeguards for removing plugins that provide media storage."""

from __future__ import annotations

from src.config.config import settings
from src.model import DownloadClient, Media, MediaLibrary
from src.plugins.loader import PluginLoadError, check_plugin_dir
from src.plugins.manager import PluginManager
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_EXTENSION_KEY,
    MEDIA_PROVIDER_REGISTRY,
)


class PluginInUseError(ValueError):
    """A media-provider plugin still owns configured media libraries."""

    def __init__(
        self,
        *,
        plugin_id: str,
        provider_keys: tuple[str, ...],
        library_ids: list[int],
        media_count: int,
        download_client_count: int,
    ) -> None:
        self.details = {
            "plugin_id": plugin_id,
            "provider_keys": list(provider_keys),
            "library_ids": library_ids,
            "media_count": media_count,
            "download_client_count": download_client_count,
        }
        super().__init__(
            f"插件仍被 {len(library_ids)} 个媒体库引用"
            f"（{media_count} 个媒体、{download_client_count} 个下载客户端），无法删除；"
            "请先迁移或删除相关媒体库。"
        )


class PluginRemovalService:
    """Delete a plugin only after its media-provider usage has been checked."""

    @classmethod
    def remove(cls, plugin_id: str) -> None:
        manager = PluginManager()
        cls._ensure_not_in_use(manager, plugin_id)
        manager.remove(plugin_id)

    @classmethod
    def _ensure_not_in_use(cls, manager: PluginManager, plugin_id: str) -> None:
        provider_keys = cls._provider_keys(manager, plugin_id)
        if not provider_keys:
            return
        libraries = list(
            MediaLibrary.select(MediaLibrary.id).where(
                MediaLibrary.provider_key.in_(provider_keys)
            )
        )
        if not libraries:
            return
        library_ids = [int(library.id) for library in libraries]
        media_count = Media.select().where(Media.library.in_(library_ids)).count()
        download_client_count = (
            DownloadClient.select()
            .where(DownloadClient.library.in_(library_ids))
            .count()
        )
        raise PluginInUseError(
            plugin_id=plugin_id,
            provider_keys=provider_keys,
            library_ids=library_ids,
            media_count=media_count,
            download_client_count=download_client_count,
        )

    @staticmethod
    def _provider_keys(manager: PluginManager, plugin_id: str) -> tuple[str, ...]:
        active_keys = MEDIA_PROVIDER_REGISTRY.provider_keys_for_plugin(plugin_id)
        if active_keys:
            return active_keys
        try:
            registration = check_plugin_dir(
                plugin_dir=manager.root_dir / plugin_id,
                plugin_settings=settings.plugins,
            )
        except (OSError, PluginLoadError, ValueError):
            return ()
        return tuple(
            sorted(
                str(extension.data.provider_key)
                for extension in registration.extensions
                if extension.key == MEDIA_PROVIDER_EXTENSION_KEY
            )
        )
