"""提交前识别 BT 资源，供宿主黑名单匹配。"""

import base64
import re
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from src.api.exception.errors import ApiError

MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_HTTP_REDIRECTS = 5


def canonical_info_hash(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", value):
        return value.lower()
    if re.fullmatch(r"[A-Za-z2-7]{32}", value):
        return base64.b32decode(value.upper()).hex()
    raise ApiError(422, "invalid_download_resource_hash", "资源缺少有效的 BT hash")


def _magnet_hash(source_uri: str) -> str:
    match = re.search(r"urn:btih:([A-Za-z0-9]+)", unquote(source_uri), re.IGNORECASE)
    if match is None:
        raise ApiError(
            422, "invalid_download_resource_hash", "磁力链接缺少有效的 BT hash"
        )
    return canonical_info_hash(match.group(1))


_MAGNET_NAME_PATTERN = re.compile(r"[?&]dn=([^&]*)", re.IGNORECASE)


def _magnet_name(source_uri: str) -> str | None:
    match = _MAGNET_NAME_PATTERN.search(source_uri)
    if match is None:
        return None
    name = unquote(match.group(1)).strip()
    return name or None


def resolve_magnet(source_uri: str) -> tuple[str, str | None]:
    """解析磁力链接；手动提交只允许磁力，种子文件走上传接口。"""
    if not source_uri.lower().startswith("magnet:"):
        raise ApiError(
            422, "invalid_download_source", "仅支持磁力链接，请上传种子文件"
        )
    return _magnet_hash(source_uri), _magnet_name(source_uri)


def _torrent_info(payload: bytes) -> tuple[str, str | None]:
    import libtorrent as lt

    try:
        info = lt.torrent_info(payload)
        if not info.info_hashes().has_v1():
            raise ValueError("missing v1 hash")
        raw_name = info.name()
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        return canonical_info_hash(str(info.info_hash())), name or None
    except (RuntimeError, ValueError) as exc:
        raise ApiError(
            422, "invalid_download_torrent", "种子文件无效或缺少 BT v1 hash"
        ) from exc


def resolve_torrent_upload(payload: bytes) -> tuple[str, str | None]:
    """解析页面上传的 .torrent 字节，返回 info hash 与种子内名称。"""
    if not payload:
        raise ApiError(422, "invalid_download_torrent", "种子文件为空")
    if len(payload) > MAX_TORRENT_BYTES:
        raise ApiError(422, "download_torrent_too_large", "种子文件超过大小限制")
    return _torrent_info(payload)


def resolve_resource(source_uri: str) -> tuple[str, str | None]:
    """解析索引器给出的磁力/种子 URL，返回 info hash 与可用的种子名称。"""
    source_uri = source_uri.strip()
    if source_uri.lower().startswith("magnet:"):
        return resolve_magnet(source_uri)
    try:
        with httpx.Client(
            timeout=120.0, follow_redirects=False, trust_env=False
        ) as client:
            for _ in range(MAX_HTTP_REDIRECTS + 1):
                parsed = urlsplit(source_uri)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                    raise ApiError(422, "invalid_download_source", "种子链接不受支持")
                with client.stream("GET", source_uri) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ApiError(
                                422, "invalid_download_source", "种子链接重定向地址无效"
                            )
                        source_uri = urljoin(source_uri, location)
                        if source_uri.lower().startswith("magnet:"):
                            return resolve_magnet(source_uri)
                        continue
                    if response.status_code == 404:
                        raise ApiError(
                            404, "download_source_not_found", "种子文件不存在"
                        )
                    if response.status_code >= 500:
                        raise ApiError(
                            503, "download_source_unavailable", "种子文件服务暂不可用"
                        )
                    if not 200 <= response.status_code < 300:
                        raise ApiError(
                            422, "invalid_download_source", "种子文件获取失败"
                        )
                    payload = bytearray()
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        if len(payload) + len(chunk) > MAX_TORRENT_BYTES:
                            raise ApiError(
                                422,
                                "download_torrent_too_large",
                                "种子文件超过大小限制",
                            )
                        payload.extend(chunk)
                    return _torrent_info(bytes(payload))
        raise ApiError(422, "invalid_download_source", "种子链接重定向次数过多")
    except httpx.HTTPError as exc:
        raise ApiError(503, "download_source_unavailable", "种子文件获取失败") from exc


def resolve_resource_hash(source_uri: str) -> str:
    return resolve_resource(source_uri)[0]
