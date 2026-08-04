import hashlib
import hmac
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from src.api.exception.errors import ApiError
from src.common.subtitle_paths import ensure_movie_subtitle_path
from src.config.config import settings

IMAGE_FILE_ROUTE_PREFIX = "/files/images"
MEDIA_STREAM_ROUTE_PREFIX = "/media"
MEDIA_CLIP_STREAM_ROUTE_PREFIX = "/media-clips"
SUBTITLE_FILE_ROUTE_PREFIX = "/files/subtitles"
FILE_SIGNATURE_EXPIRE_SECONDS = 12 * 60 * 60
# 签名过期时间戳向上对齐到该窗口边界，使同一窗口内签出的 URL 完全一致，
# 让浏览器/CDN 能真正命中缓存；向上取整保证实际有效期不低于 FILE_SIGNATURE_EXPIRE_SECONDS。
FILE_SIGNATURE_ALIGN_WINDOW_SECONDS = 6 * 60 * 60


def _now_timestamp() -> int:
    return int(time.time())


def build_signature_expires() -> int:
    """生成窗口对齐的签名过期时间戳。

    实际有效期落在 [FILE_SIGNATURE_EXPIRE_SECONDS, FILE_SIGNATURE_EXPIRE_SECONDS + 窗口)
    区间内，永远不会短于固定有效期，因此不存在签出即过期的边界问题。
    """
    target = _now_timestamp() + FILE_SIGNATURE_EXPIRE_SECONDS
    window = FILE_SIGNATURE_ALIGN_WINDOW_SECONDS
    return -(-target // window) * window


def _image_root_path() -> Path:
    image_root_path = Path(settings.media.import_image_root_path).expanduser()
    if not image_root_path.is_absolute():
        image_root_path = Path.cwd() / image_root_path
    return image_root_path.resolve()


def media_clip_root_path() -> Path:
    clip_root_path = Path(settings.media.media_clip_root_path).expanduser()
    if not clip_root_path.is_absolute():
        clip_root_path = Path.cwd() / clip_root_path
    return clip_root_path.resolve()


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
    # 资源签名有效期固定为 12 小时，不通过配置暴露；过期时间戳按窗口对齐以便前端缓存。
    expires = build_signature_expires()
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
    # 媒体播放签名与图片、字幕共用固定有效期与窗口对齐策略。
    expires = build_signature_expires()
    signature = _build_media_signature(media_id, expires)
    return f"{MEDIA_STREAM_ROUTE_PREFIX}/{media_id}/stream?expires={expires}&signature={signature}"


def build_signed_merged_media_url(media_ids: list[int]) -> str:
    """多分段虚拟合并播放的签名 URL。

    签名复用 ``media_ids[0]`` 的媒体签名（与 ``/{media_id}/stream`` 同机制），
    合并被限制在同一部影片内，实际可读集合不超过该影片各分段本身。
    """
    if not media_ids:
        raise ValueError("media_ids must not be empty")
    expires = build_signature_expires()
    signature = _build_media_signature(media_ids[0], expires)
    ids = ",".join(str(i) for i in media_ids)
    return (
        f"{MEDIA_STREAM_ROUTE_PREFIX}/merged-stream"
        f"?media_ids={ids}&expires={expires}&signature={signature}"
    )


def build_signed_cloud115_merged_hls_url(media_ids: list[int]) -> str:
    """115 HLS 全量代理的合播/单播 m3u8 签名 URL。

    签名复用 ``media_ids[0]`` 的媒体签名（与本地合并同机制），供外部播放器经
    ``/media/merged-stream.m3u8`` 消费合播 HLS（单播可由前端把 ``/stream`` 路径
    换成 ``/stream.m3u8`` 复用同一签名，无需本函数）。
    """
    if not media_ids:
        raise ValueError("media_ids must not be empty")
    expires = build_signature_expires()
    signature = _build_media_signature(media_ids[0], expires)
    ids = ",".join(str(i) for i in media_ids)
    return (
        f"{MEDIA_STREAM_ROUTE_PREFIX}/merged-stream.m3u8"
        f"?media_ids={ids}&expires={expires}&signature={signature}"
    )


def verify_media_signature(media_id: int, expires: int, signature: str) -> None:
    if expires <= _now_timestamp():
        raise ApiError(403, "file_signature_expired", "文件签名已过期")

    expected_signature = _build_media_signature(media_id, expires)
    if not hmac.compare_digest(expected_signature, signature):
        raise ApiError(403, "file_signature_invalid", "文件签名无效")


def _build_clip_signature(clip_id: int, expires: int) -> str:
    signature_payload = f"clip:{clip_id}:{expires}"
    return hmac.new(
        settings.auth.file_signature_secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_clip_url(clip_id: int) -> str:
    # 片段串流签名与其它资源共用固定有效期与窗口对齐策略。
    expires = build_signature_expires()
    signature = _build_clip_signature(clip_id, expires)
    return f"{MEDIA_CLIP_STREAM_ROUTE_PREFIX}/{clip_id}/stream?expires={expires}&signature={signature}"


def verify_clip_signature(clip_id: int, expires: int, signature: str) -> None:
    if expires <= _now_timestamp():
        raise ApiError(403, "file_signature_expired", "文件签名已过期")

    expected_signature = _build_clip_signature(clip_id, expires)
    if not hmac.compare_digest(expected_signature, signature):
        raise ApiError(403, "file_signature_invalid", "文件签名无效")


def resolve_media_clip_file_path(clip_id: int) -> Path:
    from src.model import MediaClip

    clip = MediaClip.get_or_none(MediaClip.id == clip_id)
    if clip is None:
        raise ApiError(404, "media_clip_not_found", "片段不存在")

    normalized_path = _normalize_relative_path(clip.file_path)
    clip_root_path = media_clip_root_path()
    absolute_path = (clip_root_path / normalized_path).resolve()
    # 防御性校验：解析结果必须仍在片段根目录内，避免越权读取。
    try:
        absolute_path.relative_to(clip_root_path)
    except ValueError as exc:
        raise ApiError(403, "file_path_invalid", "文件路径非法") from exc
    return absolute_path


def _build_subtitle_signature(subtitle_id: int, expires: int) -> str:
    signature_payload = f"subtitles:{subtitle_id}:{expires}"
    return hmac.new(
        settings.auth.file_signature_secret.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def build_signed_subtitle_url(subtitle_id: int) -> str:
    # 字幕下载签名与其它资源保持一致的固定有效期与窗口对齐策略。
    expires = build_signature_expires()
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
