from __future__ import annotations

import json
import os
import platform
import uuid
from pathlib import Path

import httpx
from loguru import logger
from peewee import fn

from src.config.config import Settings
from src.model import Media
from src.plugins.manager import PluginManager
from src.service.system.status_service import StatusService


class TelemetryService:
    ENDPOINT = "https://sakuramedia-telemetry.tinyping.workers.dev/v1/heartbeats"
    ENABLED_ENV_KEY = "SAKURAMEDIA_TELEMETRY_ENABLED"

    @classmethod
    def is_enabled(cls) -> bool:
        return os.getenv(cls.ENABLED_ENV_KEY, "").strip().lower() != "false"

    @classmethod
    def report(cls) -> None:
        if not cls.is_enabled():
            return
        try:
            response = httpx.post(cls.ENDPOINT, json=cls._build_payload(), timeout=10.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Telemetry heartbeat failed detail={}", exc)

    @classmethod
    def _build_payload(cls) -> dict[str, object]:
        managed_media_file_count, managed_media_total_bytes = cls._managed_media_metrics()
        return {
            "schema_version": 2,
            "instance_id": cls._load_or_create_instance_id(),
            "backend_version": (
                os.getenv(StatusService.BACKEND_VERSION_ENV_KEY)
                or StatusService.BACKEND_VERSION_DEFAULT
            ),
            "plugins": [
                {"id": plugin["plugin_id"], "version": plugin["version"]}
                for plugin in PluginManager().list_plugins()
            ],
            "platform": cls._runtime_platform(),
            "cpu_architecture": cls._cpu_architecture(),
            "managed_media_file_count": managed_media_file_count,
            "managed_media_total_bytes": managed_media_total_bytes,
        }

    @staticmethod
    def _runtime_platform() -> str:
        return platform.system().lower()

    @staticmethod
    def _cpu_architecture() -> str:
        return platform.machine()

    @staticmethod
    def _managed_media_metrics() -> tuple[int, int]:
        media_file_count, media_total_bytes = (
            Media.select(
                fn.COUNT(Media.id),
                fn.COALESCE(fn.SUM(Media.file_size_bytes), 0),
            )
            .where(Media.valid == True)
            .tuples()
            .get()
        )
        return int(media_file_count), int(media_total_bytes)

    @staticmethod
    def _load_or_create_instance_id() -> str:
        state_path = Path(Settings.model_config["toml_file"]).with_name("telemetry.json")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            return str(uuid.UUID(state["instance_id"]))
        except (FileNotFoundError, KeyError, OSError, TypeError, ValueError):
            instance_id = str(uuid.uuid4())
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"instance_id": instance_id}) + "\n", encoding="utf-8"
            )
            return instance_id
