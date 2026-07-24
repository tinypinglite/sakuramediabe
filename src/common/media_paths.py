import hashlib
import re
from pathlib import Path, PurePosixPath

from src.config.config import settings


# 影片资产在图片根下的一级目录名，与 videos/、actors/ 平级。
MOVIE_ASSETS_SUBDIR = "movies"
# 番号目录内部的保留子目录名：缩略图按 media/<内容指纹>/thumbnails 归档，字幕平铺在 subtitles/。
MOVIE_MEDIA_SUBDIR = "media"
MOVIE_SUBTITLES_SUBDIR = "subtitles"

# 分片目录名取 sha1 十六进制前 2 位，固定 256 片；顶层 movies/ 的条目数从番号数降到常数 256。
MOVIE_ASSET_SHARD_HEX_LENGTH = 2
MOVIE_ASSET_SHARD_NAMES = frozenset(
    f"{index:0{MOVIE_ASSET_SHARD_HEX_LENGTH}x}"
    for index in range(16 ** MOVIE_ASSET_SHARD_HEX_LENGTH)
)

_UNSAFE_ASSET_DIR_NAME_PATTERN = re.compile(r"[^0-9A-Za-z._-]")


def media_image_root_path() -> Path:
    """返回媒体图片根目录的规范绝对路径。"""
    image_root_path = Path(settings.media.import_image_root_path).expanduser()
    if not image_root_path.is_absolute():
        image_root_path = (Path.cwd() / image_root_path).resolve()
    return image_root_path


def normalize_asset_dir_name(owner_key: str) -> str:
    """把番号 / javdb_id 等 owner key 归一成安全目录名。

    封面、剧照、缩略图、字幕四类资产必须走同一个归一化，否则同一部影片会散到两个目录。
    """
    return _UNSAFE_ASSET_DIR_NAME_PATTERN.sub("_", owner_key).strip("._-") or "unknown"


def movie_asset_shard(dir_name: str) -> str:
    """番号资产目录的分片名：sha1 十六进制前 2 位。

    入参必须是最终落盘的目录名本身（已归一化），迁移侧与写入侧才会算出同一个分片。
    """
    return hashlib.sha1(dir_name.encode("utf-8")).hexdigest()[:MOVIE_ASSET_SHARD_HEX_LENGTH]


def movie_asset_relative_dir(dir_name: str) -> PurePosixPath:
    """番号资产目录的库内相对路径 ``movies/<shard>/<番号>``，可直接拼进 image.origin。"""
    return PurePosixPath(MOVIE_ASSETS_SUBDIR, movie_asset_shard(dir_name), dir_name)


def movie_asset_dir(movie_number: str) -> Path:
    """番号资产目录的绝对路径。"""
    return media_image_root_path() / movie_asset_relative_dir(normalize_asset_dir_name(movie_number))


def movie_subtitle_dir(movie_number: str) -> Path:
    """影片字幕统一存放目录 ``<图片根>/movies/<shard>/<番号>/subtitles``。

    本地媒体与 115 云盘媒体的字幕都落这里；媒体库内不再存放 .srt。
    """
    return movie_asset_dir(movie_number) / MOVIE_SUBTITLES_SUBDIR
