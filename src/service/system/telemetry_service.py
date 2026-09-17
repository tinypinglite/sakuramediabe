from __future__ import annotations

import json
import os
import platform
import subprocess
import uuid
from pathlib import Path

import httpx
import psutil
from loguru import logger
from peewee import fn

from src.config.config import Settings
from src.model import Media
from src.plugins.manager import PluginManager
from src.service.system.status_service import StatusService

CPU_MODEL_MAX_LENGTH = 128
CPUINFO_PATH = Path("/proc/cpuinfo")
DEVICE_TREE_MODEL_PATH = Path("/proc/device-tree/model")


class TelemetryService:
    ENDPOINTS = (
        "https://pswhnebzlzdcdljzvrqa.supabase.co/functions/v1/telemetry/v1/heartbeats",
    )
    ENABLED_ENV_KEY = "SAKURAMEDIA_TELEMETRY_ENABLED"

    @classmethod
    def is_enabled(cls) -> bool:
        return os.getenv(cls.ENABLED_ENV_KEY, "").strip().lower() != "false"

    @classmethod
    def report(cls) -> None:
        if not cls.is_enabled():
            return
        payload = cls._build_payload()

        for endpoint in cls.ENDPOINTS:
            try:
                response = httpx.post(endpoint, json=payload, timeout=10.0)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Telemetry heartbeat failed endpoint={} detail={}", endpoint, exc)

    @classmethod
    def _build_payload(cls) -> dict[str, object]:
        managed_media_file_count, managed_media_total_bytes = cls._managed_media_metrics()
        payload: dict[str, object] = {
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
        # 硬件字段为 v2 协议的可选增量，探测失败时不携带，旧遥测服务会忽略未知键。
        cpu_model = cls._cpu_model()
        if cpu_model:
            payload["cpu_model"] = cpu_model
        memory_total_bytes = cls._memory_total_bytes()
        if memory_total_bytes:
            payload["memory_total_bytes"] = memory_total_bytes
        return payload

    @classmethod
    def _cpu_model(cls) -> str | None:
        for candidate in cls._cpu_model_candidates():
            if not candidate:
                continue
            model = " ".join(candidate.split())[:CPU_MODEL_MAX_LENGTH]
            if model:
                return model
        return None

    @classmethod
    def _cpu_model_candidates(cls) -> list[str | None]:
        system = platform.system()
        if system == "Linux":
            return [cls._linux_cpu_model(), cls._device_tree_model(), platform.processor()]
        if system == "Darwin":
            return [
                cls._sysctl("machdep.cpu.brand_string"),
                cls._sysctl("hw.model"),
                platform.processor(),
            ]
        if system == "Windows":
            return [cls._windows_cpu_model(), platform.processor()]
        return [platform.processor()]

    @staticmethod
    def _linux_cpu_model() -> str | None:
        try:
            cpuinfo = CPUINFO_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        for line in cpuinfo.splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip().lower() == "model name":
                return value.strip() or None
        return None

    @staticmethod
    def _device_tree_model() -> str | None:
        try:
            value = DEVICE_TREE_MODEL_PATH.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return value.rstrip("\x00").strip() or None

    @staticmethod
    def _sysctl(name: str) -> str | None:
        try:
            result = subprocess.run(
                ["sysctl", "-n", name], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None

    @staticmethod
    def _windows_cpu_model() -> str | None:
        try:
            import winreg
        except ImportError:
            return None
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
        except OSError:
            return None
        return str(value).strip() or None

    @staticmethod
    def _memory_total_bytes() -> int | None:
        try:
            total = int(psutil.virtual_memory().total)
        except (psutil.Error, OSError, TypeError, ValueError):
            return None
        return total or None

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
