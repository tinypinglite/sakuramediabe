"""文件系统浏览与路径安全工具。

供可视化媒体导入复用：归一化绝对路径、敏感目录黑名单校验、视频文件判定。
视频后缀集合在这里作为唯一来源，导入 service 直接复用，避免重复定义。
"""

from pathlib import Path
from typing import Iterable

from src.api.exception.errors import ApiError

# 支持导入的视频文件后缀，作为浏览与导入扫描的共享唯一来源。
SUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {
        ".m2ts",
        ".mkv",
        ".mp4",
    }
)


def normalize_abs_path(raw: str) -> Path:
    """把用户传入的路径归一化为存在的绝对路径，非法或不存在时抛 ApiError。"""
    normalized_raw = (raw or "").strip()
    if not normalized_raw:
        raise ApiError(400, "path_invalid", "路径不能为空")

    candidate = Path(normalized_raw).expanduser()
    if not candidate.is_absolute():
        raise ApiError(400, "path_invalid", "路径必须是绝对路径")

    resolved = candidate.resolve()
    if not resolved.exists():
        raise ApiError(404, "path_not_found", "路径不存在", {"path": str(resolved)})
    return resolved


def assert_not_blacklisted(path: Path, blacklist: Iterable[str]) -> None:
    """路径命中黑名单目录本身或其子树时拒绝访问。"""
    resolved_path = path.expanduser().resolve()
    for raw_root in blacklist:
        normalized_root = (raw_root or "").strip()
        if not normalized_root:
            continue
        blacklist_root = Path(normalized_root).expanduser().resolve()
        if resolved_path == blacklist_root or resolved_path.is_relative_to(blacklist_root):
            raise ApiError(
                403,
                "path_forbidden",
                "该目录禁止访问",
                {"path": str(resolved_path)},
            )


def is_video_file(path: Path) -> bool:
    """按后缀判定是否为受支持的视频文件。"""
    return path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
