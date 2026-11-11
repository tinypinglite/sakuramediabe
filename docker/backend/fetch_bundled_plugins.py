"""解析并下载随镜像内置的官方插件包。

版本不写死：仓库里只维护「插件 → GitHub 仓库」清单，每次构建都取各仓库的 latest 正式
Release；需要复现历史镜像或临时回滚某个插件时用 ``--pin <plugin_id>=v<version>`` 固定。

两种模式：
- ``resolve``：只解析并打印本次将内置的版本与资产地址，供发版前人工核查（开发态预检）；
- ``fetch``：镜像构建期下载 ZIP、校验 manifest 与 sha256，产出宿主安装索引。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GITHUB_API_ROOT = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 60.0
# tag 约定 v<version>；插件发版流程已保证 manifest.version == tag 去掉 v。
_TAG_PATTERN = re.compile(r"^v(?P<version>[0-9][0-9A-Za-z.+-]*)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
MANIFEST_FILENAME = "manifest.json"
PACKAGE_INIT_FILENAME = "__init__.py"
OFFICIAL_PLUGINS_INDEX_NAME = "official-plugins.json"
BUILD_INFO_NAME = "BUILD-INFO.json"


@dataclass(frozen=True)
class BundledPluginSpec:
    """一个内置插件：宿主 plugin_id 与它对应的 GitHub 仓库。"""

    plugin_id: str
    repo: str


@dataclass(frozen=True)
class ResolvedPlugin:
    """解析结果：本次构建将内置的具体版本与资产地址。"""

    plugin_id: str
    repo: str
    tag: str
    version: str
    asset_name: str
    asset_url: str
    checksum_url: str | None


# Host API 18 is a compatibility reset. Keep the image free of old plugin
# releases until the official plugins publish matching Host API 18 builds.
BUNDLED_PLUGINS: tuple[BundledPluginSpec, ...] = ()


class BundledPluginError(RuntimeError):
    """解析或下载内置插件失败；消息带 plugin_id，便于定位是哪一条出问题。"""


HttpGet = Callable[[str], bytes]


def _request_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sakuramediabe-bundled-plugins",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def build_http_get(token: str | None) -> HttpGet:
    """构造真实 HTTP 取回函数；测试用假函数替换即可离线单测。"""

    def http_get(url: str) -> bytes:
        request = urllib.request.Request(url, headers=_request_headers(token))
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise BundledPluginError(
                f"http_error: status={exc.code} url={url}"
            ) from exc
        except urllib.error.URLError as exc:
            raise BundledPluginError(
                f"network_error: url={url} reason={exc.reason}"
            ) from exc

    return http_get


def _load_json(payload: bytes, *, url: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundledPluginError(f"response_invalid_json: url={url}") from exc
    if not isinstance(value, dict):
        raise BundledPluginError(f"response_invalid_json: url={url} expected object")
    return value


def _asset_urls(payload: dict[str, Any], *, plugin_id: str) -> dict[str, str]:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise BundledPluginError(f"release_invalid: plugin_id={plugin_id} missing assets")
    urls: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if isinstance(name, str) and isinstance(url, str):
            urls[name] = url
    return urls


def _release_api_url(spec: BundledPluginSpec, pinned_tag: str | None) -> str:
    if pinned_tag is None:
        return f"{GITHUB_API_ROOT}/repos/{spec.repo}/releases/latest"
    return f"{GITHUB_API_ROOT}/repos/{spec.repo}/releases/tags/{pinned_tag}"


def _parse_release(
    spec: BundledPluginSpec,
    payload: dict[str, Any],
    *,
    pinned: bool,
) -> ResolvedPlugin:
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise BundledPluginError(
            f"release_invalid: plugin_id={spec.plugin_id} missing tag_name"
        )
    if payload.get("draft"):
        raise BundledPluginError(
            f"release_invalid: plugin_id={spec.plugin_id} tag={tag} draft=true"
        )
    # 自动挑选只认正式版；显式 pin 到的 tag 允许是预发布，便于临时排查。
    if payload.get("prerelease") and not pinned:
        raise BundledPluginError(
            f"release_invalid: plugin_id={spec.plugin_id} tag={tag} prerelease=true"
        )
    match = _TAG_PATTERN.match(tag)
    if match is None:
        raise BundledPluginError(
            f"release_invalid: plugin_id={spec.plugin_id} tag={tag} "
            "expected v<version>"
        )
    version = match.group("version")
    asset_name = f"{spec.plugin_id}-{version}.zip"
    urls = _asset_urls(payload, plugin_id=spec.plugin_id)
    asset_url = urls.get(asset_name)
    if asset_url is None:
        raise BundledPluginError(
            f"release_asset_missing: plugin_id={spec.plugin_id} tag={tag} "
            f"expected={asset_name} available={sorted(urls)}"
        )
    if not asset_url.startswith("https://"):
        raise BundledPluginError(
            f"release_asset_invalid: plugin_id={spec.plugin_id} url={asset_url}"
        )
    return ResolvedPlugin(
        plugin_id=spec.plugin_id,
        repo=spec.repo,
        tag=tag,
        version=version,
        asset_name=asset_name,
        asset_url=asset_url,
        checksum_url=urls.get(f"{asset_name}.sha256"),
    )


def _resolve_plugin(
    spec: BundledPluginSpec,
    *,
    pinned_tag: str | None,
    http_get: HttpGet,
) -> ResolvedPlugin:
    url = _release_api_url(spec, pinned_tag)
    return _parse_release(
        spec,
        _load_json(http_get(url), url=url),
        pinned=pinned_tag is not None,
    )


def parse_pins(values: list[str]) -> dict[str, str]:
    """把 ``sakuramedia_x=v0.2.6`` 解析成 {plugin_id: tag}。"""
    pins: dict[str, str] = {}
    known = {spec.plugin_id for spec in BUNDLED_PLUGINS}
    for value in values:
        plugin_id, separator, tag = value.partition("=")
        plugin_id = plugin_id.strip()
        tag = tag.strip()
        if not separator or not plugin_id or not tag:
            raise BundledPluginError(f"invalid_pin: {value} expected <plugin_id>=v<version>")
        if plugin_id not in known:
            raise BundledPluginError(f"invalid_pin: unknown plugin_id={plugin_id}")
        if _TAG_PATTERN.match(tag) is None:
            raise BundledPluginError(f"invalid_pin: {value} expected tag v<version>")
        pins[plugin_id] = tag
    return pins


def resolve_plugins(
    *,
    http_get: HttpGet,
    pins: dict[str, str] | None = None,
) -> list[ResolvedPlugin]:
    """按插件清单顺序解析全部插件的目标版本。"""
    pins = pins or {}
    return [
        _resolve_plugin(spec, pinned_tag=pins.get(spec.plugin_id), http_get=http_get)
        for spec in BUNDLED_PLUGINS
    ]


def _read_manifest_from_archive(payload: bytes, *, url: str) -> dict[str, Any]:
    """从插件 ZIP 根部读 manifest，并要求包根带 __init__.py。"""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            raw = archive.read(MANIFEST_FILENAME)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise BundledPluginError(
            f"archive_invalid: url={url} expected root {MANIFEST_FILENAME}"
        ) from exc
    if PACKAGE_INIT_FILENAME not in names:
        raise BundledPluginError(
            f"archive_invalid: url={url} missing root {PACKAGE_INIT_FILENAME}"
        )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundledPluginError(f"archive_invalid: url={url} manifest not json") from exc
    if not isinstance(manifest, dict):
        raise BundledPluginError(
            f"archive_invalid: url={url} manifest must be an object"
        )
    return manifest


def _parse_published_sha256(payload: bytes, *, url: str) -> str:
    """解析 ``sha256sum`` 输出（``<hex>  <filename>``）。"""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundledPluginError(f"checksum_invalid: url={url}") from exc
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        digest = fields[0]
        if _SHA256_PATTERN.match(digest) is None:
            raise BundledPluginError(f"checksum_invalid: url={url} value={digest}")
        return digest.lower()
    raise BundledPluginError(f"checksum_invalid: url={url} empty")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _reset_output_dir(output_dir: Path) -> None:
    """清掉上一次构建的同名产物，避免残留 ZIP 混入镜像。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in (
        output_dir / OFFICIAL_PLUGINS_INDEX_NAME,
        output_dir / BUILD_INFO_NAME,
        *output_dir.glob("*.zip"),
    ):
        stale.unlink(missing_ok=True)


def fetch_plugins(
    *,
    output_dir: Path,
    http_get: HttpGet,
    pins: dict[str, str] | None = None,
) -> tuple[list[ResolvedPlugin], Path, Path]:
    """下载并校验全部内置插件，产出安装索引与构建信息。"""
    resolved = resolve_plugins(http_get=http_get, pins=pins)
    output_dir = Path(output_dir)
    _reset_output_dir(output_dir)
    index_plugins: list[dict[str, str]] = []
    build_info_plugins: list[dict[str, str]] = []
    for item in resolved:
        payload = http_get(item.asset_url)
        manifest = _read_manifest_from_archive(payload, url=item.asset_url)
        if manifest.get("plugin_id") != item.plugin_id:
            raise BundledPluginError(
                "archive_manifest_mismatch: "
                f"plugin_id={item.plugin_id} manifest={manifest.get('plugin_id')}"
            )
        if str(manifest.get("version")) != item.version:
            raise BundledPluginError(
                "archive_manifest_mismatch: "
                f"plugin_id={item.plugin_id} tag={item.tag} "
                f"manifest={manifest.get('version')}"
            )
        digest = hashlib.sha256(payload).hexdigest()
        if item.checksum_url is not None:
            # provider 发布了 sha256 资产时交叉比对，防止下载链路损坏而 manifest 仍可读。
            published = _parse_published_sha256(
                http_get(item.checksum_url), url=item.checksum_url
            )
            if published != digest:
                raise BundledPluginError(
                    "archive_checksum_mismatch: "
                    f"plugin_id={item.plugin_id} published={published} actual={digest}"
                )
        (output_dir / item.asset_name).write_bytes(payload)
        index_plugins.append(
            {
                "plugin_id": item.plugin_id,
                "filename": item.asset_name,
                "version": item.version,
                "sha256": digest,
            }
        )
        build_info_plugins.append(
            {
                "plugin_id": item.plugin_id,
                "repo": item.repo,
                "tag": item.tag,
                "version": item.version,
                "filename": item.asset_name,
                "sha256": digest,
            }
        )

    index_path = output_dir / OFFICIAL_PLUGINS_INDEX_NAME
    _write_json(index_path, {"version": 1, "plugins": index_plugins})
    build_info_path = output_dir / BUILD_INFO_NAME
    _write_json(
        build_info_path,
        {
            "version": 1,
            "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "plugins": build_info_plugins,
        },
    )
    return resolved, index_path, build_info_path


def _format_resolved(item: ResolvedPlugin) -> str:
    checksum = item.checksum_url or "-"
    return (
        f"plugin_id={item.plugin_id} tag={item.tag} version={item.version} "
        f"asset={item.asset_url} checksum={checksum}"
    )


def _format_fetched(item: ResolvedPlugin, *, output_dir: Path) -> str:
    return (
        f"plugin_id={item.plugin_id} tag={item.tag} version={item.version} "
        f"file={output_dir / item.asset_name}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve and fetch the official plugin releases bundled into the image."
    )
    parser.add_argument(
        "--mode",
        choices=("resolve", "fetch"),
        default="resolve",
        help="resolve: print resolved versions; fetch: download and verify archives.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("bundled"),
        help="Output directory for --mode fetch (default: ./bundled).",
    )
    parser.add_argument(
        "--pin",
        action="append",
        default=[],
        metavar="PLUGIN_ID=vVERSION",
        help="Pin one plugin to an explicit release tag; repeatable.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN") or None,
        help="GitHub token for API requests; defaults to $GITHUB_TOKEN.",
    )
    args = parser.parse_args(argv)

    http_get = build_http_get(args.token)
    try:
        pins = parse_pins(args.pin)
        if args.mode == "resolve":
            resolved = resolve_plugins(http_get=http_get, pins=pins)
            for item in resolved:
                print(_format_resolved(item))
            return 0
        resolved, index_path, build_info_path = fetch_plugins(
            output_dir=args.output,
            http_get=http_get,
            pins=pins,
        )
    except BundledPluginError as exc:
        print(f"bundled plugins error: {exc}", file=sys.stderr)
        return 1

    for item in resolved:
        print(_format_fetched(item, output_dir=args.output))
    print(f"index={index_path} build_info={build_info_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
