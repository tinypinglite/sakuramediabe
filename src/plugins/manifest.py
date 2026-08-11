"""插件包声明（manifest）模型与校验。

manifest.json 是插件包的唯一声明入口：标识、版本、宿主接口版本、
Python 版本约束、运行时依赖与包内文件哈希。安装器、依赖管理器、
loader 全部以本模型为准，避免各自解析 JSON 造成口径漂移。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
MANIFEST_FILENAME = "manifest.json"


class PluginDependencies(BaseModel):
    """插件声明的运行时依赖；由宿主统一托管安装。"""

    model_config = ConfigDict(extra="forbid")

    requirements: list[str] = Field(default_factory=list)
    # 自定义 pip index；留空使用 pip 默认源。
    index_url: str | None = None
    extra_index_urls: list[str] = Field(default_factory=list)
    # 优先从包内 wheels/ 目录安装（离线部署兜底），缺失的包再走 index。
    bundled_wheels: bool = False


class PluginManifest(BaseModel):
    """插件包 manifest.json 的严格模型，未知字段直接拒绝。"""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    host_api_version: int = Field(ge=1)
    requires_python: str | None = None
    dependencies: PluginDependencies = Field(default_factory=PluginDependencies)
    # 包内每个文件的 sha256；解压后逐文件校验，防包内容被篡改。
    files: dict[str, str] = Field(default_factory=dict)
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

    @field_validator("files")
    @classmethod
    def _validate_file_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        for file_path, digest in value.items():
            if "/" in file_path or "\\" in file_path or not digest:
                raise ValueError(f"manifest.files 键必须是相对文件名且哈希非空: {file_path}")
            if len(digest) != 64:
                raise ValueError(f"manifest.files 哈希必须是 sha256 十六进制: {file_path}")
        return value

    def dependencies_digest(self) -> str:
        """依赖声明的规范化摘要，供 installed.json 判断是否需要重装。"""
        payload = json.dumps(
            self.dependencies.model_dump(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_manifest_from_dict(data: dict[str, Any]) -> PluginManifest:
    return PluginManifest.model_validate(data)


def load_manifest_from_file(path: Path) -> PluginManifest:
    """读取已安装插件目录下的 manifest.json。"""
    manifest_path = path / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise ValueError(f"插件缺少 {MANIFEST_FILENAME}: {path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"插件 manifest.json 无法解析: {path}") from exc
    return load_manifest_from_dict(data)
