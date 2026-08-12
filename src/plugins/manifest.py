"""插件声明（manifest）模型与校验。

manifest.json 是插件目录的唯一声明入口：标识、版本、宿主接口版本、
Python 版本约束与展示信息。插件就是插件根目录下的一个子目录，
没有 zip 打包、依赖声明或文件哈希。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MANIFEST_FILENAME = "manifest.json"


class PluginManifest(BaseModel):
    """插件 manifest.json 的严格模型，未知字段直接拒绝。"""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    host_api_version: int = Field(ge=1)
    requires_python: str | None = None
    author: str | None = None
    homepage: str | None = None

    @field_validator("plugin_id")
    @classmethod
    def _validate_plugin_id(cls, value: str) -> str:
        if not PLUGIN_ID_PATTERN.fullmatch(value):
            raise ValueError(
                f"plugin_id 只能包含小写字母、数字、下划线且必须以字母开头: {value}"
            )
        return value


def load_manifest_from_dict(data: dict[str, Any]) -> PluginManifest:
    return PluginManifest.model_validate(data)


def load_manifest_from_file(path: Path) -> PluginManifest:
    """读取插件目录下的 manifest.json。"""
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ValueError(f"插件缺少 {MANIFEST_FILENAME}: {path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"插件 manifest.json 无法解析: {path}") from exc
    return load_manifest_from_dict(data)
