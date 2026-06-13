"""文件系统浏览与路径安全工具。

供可视化媒体导入复用：归一化绝对路径、白名单根目录校验、视频文件判定。
浏览/导入/删除/重命名等一切接受用户路径的入口，都必须先 ``normalize_abs_path``
再 ``assert_within_allowed_roots``，确保解析后的真实路径落在配置的白名单根之内。
视频后缀集合在这里作为唯一来源，导入 service 直接复用，避免重复定义。
"""

from pathlib import Path
from typing import Iterable, List

from src.api.exception.errors import ApiError

# 支持导入的视频文件后缀，作为浏览与导入扫描的共享唯一来源（JAV 与非 JAV 共用，保持一致）。
# 覆盖日本 AV 资源常见的容器/封装格式：主流现代封装、老资源常见的 wmv/avi/rm 系列，以及
# 蓝光/DVD 原盘衍生的 ts/m2ts/mts/vob/iso。注意 iso/vob 为盘镜像/DVD 流，能登记入库，但探测/缩略图
# 依赖 PyAV 解码能力，原盘类文件可能无法生成缩略图（仅记失败，不影响入库）。
SUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {
        # 主流现代封装
        ".mp4",
        ".mkv",
        ".mov",
        ".m4v",
        ".webm",
        ".ts",
        ".m2ts",
        ".mts",
        ".avi",
        ".wmv",
        ".flv",
        ".f4v",
        ".mpg",
        ".mpeg",
        ".rm",
        ".rmvb",
        ".3gp",
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


def resolve_allowed_roots(roots: Iterable[str]) -> List[Path]:
    """把配置的白名单根目录归一化为去重后的绝对路径列表。"""
    resolved_roots: List[Path] = []
    seen: set[str] = set()
    for raw_root in roots:
        normalized_root = (raw_root or "").strip()
        if not normalized_root:
            continue
        root_path = Path(normalized_root).expanduser().resolve()
        key = str(root_path)
        if key in seen:
            continue
        seen.add(key)
        resolved_roots.append(root_path)
    return resolved_roots


def is_within_allowed_roots(path: Path, roots: Iterable[str]) -> bool:
    """判断解析后的真实路径是否落在任一白名单根目录（含其自身或子树）内。"""
    resolved_path = path.expanduser().resolve()
    for root_path in resolve_allowed_roots(roots):
        if resolved_path == root_path or resolved_path.is_relative_to(root_path):
            return True
    return False


def assert_within_allowed_roots(path: Path, roots: Iterable[str]) -> None:
    """路径解析后不在白名单根目录范围内时拒绝访问。"""
    if not is_within_allowed_roots(path, roots):
        raise ApiError(
            403,
            "path_forbidden",
            "该目录不在允许的浏览范围内",
            {"path": str(path.expanduser().resolve())},
        )


def is_video_file(path: Path) -> bool:
    """按后缀判定是否为受支持的视频文件。"""
    return path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
