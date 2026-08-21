from __future__ import annotations

from pathlib import Path

from src.api.exception.errors import ApiError
from src.common.media_paths import MOVIE_SUBTITLE_EXTENSIONS, movie_subtitle_dir


def normalize_subtitle_path(file_path: str | Path) -> Path:
    absolute_path = Path(file_path).expanduser()
    if not absolute_path.is_absolute():
        absolute_path = (Path.cwd() / absolute_path).resolve()
    else:
        absolute_path = absolute_path.resolve()
    if absolute_path.suffix.lower() not in MOVIE_SUBTITLE_EXTENSIONS:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return absolute_path


def _is_path_within_root(file_path: Path, root_path: Path) -> bool:
    try:
        file_path.relative_to(root_path)
    except ValueError:
        return False
    return True


def ensure_movie_subtitle_path(movie, file_path: str | Path) -> Path:
    """校验字幕绝对路径位于该影片的标准字幕目录内。"""
    absolute_path = normalize_subtitle_path(file_path)
    if _is_path_within_root(absolute_path, movie_subtitle_dir(movie.movie_number).resolve()):
        return absolute_path
    raise ApiError(403, "file_path_invalid", "文件路径非法")
