from __future__ import annotations

from pathlib import Path

from src.api.exception.errors import ApiError
from src.common.media_paths import MOVIE_SUBTITLE_EXTENSIONS, movie_subtitle_dir
from src.config.config import settings


def normalize_subtitle_path(file_path: str | Path) -> Path:
    absolute_path = Path(file_path).expanduser()
    if not absolute_path.is_absolute():
        absolute_path = (Path.cwd() / absolute_path).resolve()
    else:
        absolute_path = absolute_path.resolve()
    if absolute_path.suffix.lower() not in MOVIE_SUBTITLE_EXTENSIONS:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return absolute_path


def subtitle_root_path() -> Path:
    """115 旧字幕根的绝对路径（legacy）。

    新导入不再往这里写（统一落 ``movies/<shard>/<番号>/subtitles/``），但存量 115 字幕仍在此，
    未迁移的老用户要能继续读取，故运行时仍把它当合法根之一。
    """
    root_path = Path(settings.media.subtitle_root_path).expanduser()
    if not root_path.is_absolute():
        root_path = Path.cwd() / root_path
    return root_path.resolve()


def movie_subtitle_root_path(movie_number: str) -> Path:
    """某番号在 115 旧字幕根下的目录 ``<旧字幕根>/<番号>``（legacy）。"""
    normalized_movie_number = (movie_number or "").strip()
    if not normalized_movie_number:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    if normalized_movie_number in {".", ".."}:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    if "/" in normalized_movie_number or "\\" in normalized_movie_number:
        raise ApiError(403, "file_path_invalid", "文件路径非法")
    return (subtitle_root_path() / normalized_movie_number).resolve()


def iter_movie_sidecar_roots(movie) -> list[Path]:
    """该影片本地媒体所在目录集合（legacy sidecar 根）。

    老用户的本地字幕作为 sidecar 跟视频放在媒体库版本目录里；未迁移时字幕就落在这些目录下，
    运行时要放行。cloud115 等云盘 Media 的 path 为 NULL（字幕在旧字幕根），只遍历有本地路径的行。
    """
    from src.model import Media

    sidecar_roots: list[Path] = []
    seen_paths: set[str] = set()
    media_items = (
        Media.select(Media.path)
        .where(Media.movie == movie, Media.path.is_null(False))
        .order_by(Media.id.asc())
    )
    for media in media_items:
        media_root = Path(media.path).expanduser().resolve().parent
        key = str(media_root)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        sidecar_roots.append(media_root)
    return sidecar_roots


def _is_path_within_root(file_path: Path, root_path: Path) -> bool:
    try:
        file_path.relative_to(root_path)
    except ValueError:
        return False
    return True


def ensure_movie_subtitle_path(movie, file_path: str | Path) -> Path:
    """校验字幕绝对路径落在该影片任一合法字幕根内，否则拒绝访问。

    向后兼容：同时放行**新布局**统一目录与**老布局**两处位置，未迁移的老用户照常可读，
    ``migrate-movie-subtitles`` 因此是可选整理而非硬性前置：

    - 新布局：``movies/<shard>/<番号>/subtitles/``（新导入与迁移后的落点）
    - 老布局-115：``<旧字幕根>/<番号>/``
    - 老布局-本地：媒体库里视频所在的版本目录（sidecar）
    """
    absolute_path = normalize_subtitle_path(file_path)

    if _is_path_within_root(absolute_path, movie_subtitle_dir(movie.movie_number).resolve()):
        return absolute_path
    if _is_path_within_root(absolute_path, movie_subtitle_root_path(movie.movie_number)):
        return absolute_path
    for sidecar_root in iter_movie_sidecar_roots(movie):
        if _is_path_within_root(absolute_path, sidecar_root):
            return absolute_path

    raise ApiError(403, "file_path_invalid", "文件路径非法")
