import hashlib
import hmac
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.common.subtitle_paths import ensure_movie_subtitle_path

IMAGE_FILE_ROUTE_PREFIX = "/files/images"
MEDIA_STREAM_ROUTE_PREFIX = "/media"
SUBTITLE_FILE_ROUTE_PREFIX = "/files/subtitles"
FILE_SIGNATURE_EXPIRE_SECONDS = 12 * 60 * 60


def _now_timestamp() -> int:
    return int(time.time())


def _image_root_path() -> Path:
    image_root_path = Path(settings.media.import_image_root_path).expanduser()
    if not image_root_path.is_absolute():
        image_root_path = Path.cwd() / image_root_path
    return image_root_path.resolve()


def _normalize_relative_path(relative_path: str) -> str:
    normalized_input = (relative_path or "").strip().replace("\\", "/")
    if not normalized_input or normalized_input.startswith("/"):
        raise ApiError(403, "file_path_invalid", "文件路径非法")

    raw_parts = normalized_input.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ApiError(403, "file_path_invalid", "文件路径非法")

    normalized_path = PurePosixPath(*raw_parts).as_posix()
    if not normalized_path:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return normalized_path


def _build_image_signature(relative_path: str, expires: int) -> str:
    signature_payload = f"images:{relative_path}:{expires}"
    return hmac.new(
        settings.auth.file_signature_secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_image_url(relative_path: str) -> str:
    normalized_path = _normalize_relative_path(relative_path)
    # 资源签名有效期固定为 12 小时，不通过配置暴露。
    expires = _now_timestamp() + FILE_SIGNATURE_EXPIRE_SECONDS
    signature = _build_image_signature(normalized_path, expires)
    return (
        f"{IMAGE_FILE_ROUTE_PREFIX}/{quote(normalized_path, safe='/')}"
        f"?expires={expires}&signature={signature}"
    )


def verify_image_signature(file_path: str, expires: int, signature: str) -> str:
    normalized_path = _normalize_relative_path(file_path)
    if expires <= _now_timestamp():
        raise ApiError(403, "file_signature_expired", "文件签名已过期")

    expected_signature = _build_image_signature(normalized_path, expires)
    if not hmac.compare_digest(expected_signature, signature):
        raise ApiError(403, "file_signature_invalid", "文件签名无效")
    return normalized_path


def resolve_image_file_path(relative_path: str) -> Path:
    normalized_path = _normalize_relative_path(relative_path)
    image_root_path = _image_root_path()
    absolute_path = (image_root_path / normalized_path).resolve()

    try:
        absolute_path.relative_to(image_root_path)
    except ValueError as exc:
        raise ApiError(403, "file_path_invalid", "文件路径非法") from exc

    return absolute_path


def _build_media_signature(media_id: int, expires: int) -> str:
    signature_payload = f"media:{media_id}:{expires}"
    return hmac.new(
        settings.auth.file_signature_secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_media_url(media_id: int) -> str:
    # 媒体播放签名与图片、字幕共用固定有效期。
    expires = _now_timestamp() + FILE_SIGNATURE_EXPIRE_SECONDS
    signature = _build_media_signature(media_id, expires)
    return f"{MEDIA_STREAM_ROUTE_PREFIX}/{media_id}/stream?expires={expires}&signature={signature}"


def verify_media_signature(media_id: int, expires: int, signature: str) -> None:
    if expires <= _now_timestamp():
        raise ApiError(403, "file_signature_expired", "文件签名已过期")

    expected_signature = _build_media_signature(media_id, expires)
    if not hmac.compare_digest(expected_signature, signature):
        raise ApiError(403, "file_signature_invalid", "文件签名无效")


def resolve_media_file_path(media_id: int) -> Path:
    from src.model import Media

    media = Media.get_or_none(Media.id == media_id)
    if media is None:
        raise ApiError(404, "media_not_found", "媒体不存在")
    return Path(media.path).expanduser().resolve()


def _build_subtitle_signature(subtitle_id: int, expires: int) -> str:
    signature_payload = f"subtitles:{subtitle_id}:{expires}"
    return hmac.new(
        settings.auth.file_signature_secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_subtitle_url(subtitle_id: int) -> str:
    # 字幕下载签名与其它资源保持一致的固定有效期。
    expires = _now_timestamp() + FILE_SIGNATURE_EXPIRE_SECONDS
    signature = _build_subtitle_signature(subtitle_id, expires)
    return f"{SUBTITLE_FILE_ROUTE_PREFIX}/{subtitle_id}?expires={expires}&signature={signature}"


def verify_subtitle_signature(subtitle_id: int, expires: int, signature: str) -> None:
    if expires <= _now_timestamp():
        raise ApiError(403, "file_signature_expired", "文件签名已过期")

    expected_signature = _build_subtitle_signature(subtitle_id, expires)
    if not hmac.compare_digest(expected_signature, signature):
        raise ApiError(403, "file_signature_invalid", "文件签名无效")


def resolve_subtitle_file_path(subtitle_id: int) -> Path:
    from src.model import Subtitle

    subtitle = Subtitle.get_or_none(Subtitle.id == subtitle_id)
    if subtitle is None:
        raise ApiError(404, "subtitle_not_found", "字幕不存在")

    return ensure_movie_subtitle_path(subtitle.movie, subtitle.file_path)
