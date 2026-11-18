"""Canonical host-side download source resolution."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from src.api.exception.errors import ApiError
from src.plugins.provider_protocol import DownloadSourceKind
from src.service.transfers.downloads.resource_hash import (
    resolve_magnet,
    resolve_torrent_upload,
)

_ED2K_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_HTTP_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class ResolvedDownloadSource:
    kind: DownloadSourceKind
    source_uri: str
    resource_key: str
    info_hash: str | None
    display_name: str | None


def _invalid_source(message: str) -> ApiError:
    return ApiError(422, "invalid_download_source", message)


def _source_kind(value: str | None) -> DownloadSourceKind | None:
    if value is None:
        return None
    if value not in {"magnet", "torrent", "ed2k", "http"}:
        raise _invalid_source("不支持的下载来源类型")
    return value  # type: ignore[return-value]


def _resolve_ed2k(source_uri: str) -> ResolvedDownloadSource:
    parts = source_uri.split("|")
    if (
        len(parts) < 6
        or parts[0].lower() != "ed2k://"
        or parts[1].lower() != "file"
        or not source_uri.rstrip().endswith("|/")
    ):
        raise _invalid_source("eD2k 链接必须是有效的文件链接")
    name = parts[2].strip()
    if not name:
        raise _invalid_source("eD2k 链接缺少文件名")
    try:
        size = int(parts[3])
    except ValueError as exc:
        raise _invalid_source("eD2k 链接缺少有效的文件大小") from exc
    if size <= 0:
        raise _invalid_source("eD2k 链接缺少有效的文件大小")
    info_hash = parts[4].strip().lower()
    if _ED2K_HASH_RE.fullmatch(info_hash) is None:
        raise _invalid_source("eD2k 链接缺少有效的 MD4 hash")
    return ResolvedDownloadSource(
        kind="ed2k",
        source_uri=source_uri,
        resource_key=f"ed2k:{info_hash}:{size}",
        info_hash=None,
        display_name=unquote(name),
    )


def _normalise_http_url(source_uri: str) -> str:
    parsed = urlsplit(source_uri)
    if parsed.scheme.lower() not in _HTTP_SCHEMES or not parsed.netloc:
        raise _invalid_source("HTTP 下载必须使用有效的 http/https 直链")
    if "@" in parsed.netloc:
        raise _invalid_source("HTTP 下载地址不能包含用户名或密码")
    host = parsed.hostname.lower() if parsed.hostname else ""
    if not host:
        raise _invalid_source("HTTP 下载地址缺少主机名")
    try:
        port = parsed.port
    except ValueError as exc:
        raise _invalid_source("HTTP 下载地址端口无效") from exc
    if port is None or (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    ):
        netloc = host
    else:
        netloc = f"{host}:{port}"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    )


def _resolve_http(source_uri: str) -> ResolvedDownloadSource:
    normalized = _normalise_http_url(source_uri)
    resource_key = "http:" + hashlib.sha256(normalized.encode()).hexdigest()
    path_name = unquote(urlsplit(normalized).path.rsplit("/", 1)[-1])
    return ResolvedDownloadSource(
        kind="http",
        source_uri=source_uri,
        resource_key=resource_key,
        info_hash=None,
        display_name=path_name or None,
    )


def resolve_manual_download_source(
    source_uri: str,
    *,
    source_kind: str | None = None,
    torrent_bytes: bytes | None = None,
) -> ResolvedDownloadSource:
    """Resolve one manually submitted source into a stable host identity."""
    declared_kind = _source_kind(source_kind)
    text = (source_uri or "").strip()

    if torrent_bytes is not None:
        if declared_kind not in {None, "torrent"}:
            raise _invalid_source("来源类型与种子文件不匹配")
        info_hash, torrent_name = resolve_torrent_upload(torrent_bytes)
        return ResolvedDownloadSource(
            kind="torrent",
            source_uri=f"magnet:?xt=urn:btih:{info_hash}",
            resource_key=f"bt:{info_hash}",
            info_hash=info_hash,
            display_name=torrent_name,
        )

    if not text:
        raise _invalid_source("下载链接不能为空")

    if text.lower().startswith("magnet:"):
        if declared_kind not in {None, "magnet"}:
            raise _invalid_source("来源类型与磁力链接不匹配")
        info_hash, name = resolve_magnet(text)
        return ResolvedDownloadSource(
            kind="magnet",
            source_uri=text,
            resource_key=f"bt:{info_hash}",
            info_hash=info_hash,
            display_name=name,
        )

    if text.lower().startswith("ed2k://"):
        if declared_kind not in {None, "ed2k"}:
            raise _invalid_source("来源类型与 eD2k 链接不匹配")
        return _resolve_ed2k(text)

    if text.lower().startswith(("http://", "https://")):
        if declared_kind not in {None, "http"}:
            raise _invalid_source("HTTP 链接只支持直链下载")
        return _resolve_http(text)

    raise _invalid_source("仅支持 magnet、eD2k、HTTP 直链或种子文件")


def resource_suffix(resource_key: str) -> str:
    if resource_key.startswith("bt:"):
        return resource_key[3:][:6]
    if resource_key.startswith("ed2k:"):
        _, info_hash, _size = resource_key.split(":", 2)
        return info_hash[:6]
    if resource_key.startswith("http:"):
        return resource_key[5:][:6]
    return resource_key[:6]
