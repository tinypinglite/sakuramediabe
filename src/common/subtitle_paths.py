from __future__ import annotations

from pathlib import Path

from src.api.exception.errors import ApiError
from src.config.config import settings


def normalize_subtitle_path(file_path: str | Path) -> Path:
    absolute_path = Path(file_path).expanduser()
    if not absolute_path.is_absolute():
        absolute_path = (Path.cwd() / absolute_path).resolve()
    else:
        absolute_path = absolute_path.resolve()
    if absolute_path.suffix.lower() != ".srt":
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return absolute_path


def subtitle_root_path() -> Path:
    root_path = Path(settings.media.subtitle_root_path).expanduser()
    if not root_path.is_absolute():
        root_path = Path.cwd() / root_path
    return root_path.resolve()


def movie_subtitle_root_path(movie_number: str) -> Path:
    normalized_movie_number = (movie_number or "").strip()
    if not normalized_movie_number:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    if normalized_movie_number in {".", ".."}:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    if "/" in normalized_movie_number or "\\" in normalized_movie_number:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return (subtitle_root_path() / normalized_movie_number).resolve()


def _is_path_within_root(file_path: Path, root_path: Path) -> bool:
    try:
        file_path.relative_to(root_path)
    except ValueError:
        return False
    return True


def iter_movie_sidecar_roots(movie) -> list[Path]:
    from src.model import Media

    sidecar_roots: list[Path] = []
    seen_paths: set[str] = set()
    media_items = Media.select(Media.path).where(Media.movie == movie).order_by(Media.id.asc())
    for media in media_items:
        media_root = Path(media.path).expanduser().resolve().parent
        key = str(media_root)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        sidecar_roots.append(media_root)
    return sidecar_roots


def ensure_movie_subtitle_path(movie, file_path: str | Path) -> Path:
    absolute_path = normalize_subtitle_path(file_path)
    if _is_path_within_root(absolute_path, movie_subtitle_root_path(movie.movie_number)):
        return absolute_path

    for sidecar_root in iter_movie_sidecar_roots(movie):
        if _is_path_within_root(absolute_path, sidecar_root):
            return absolute_path

    raise ApiError(403, "file_path_invalid", "文件路径非法")
