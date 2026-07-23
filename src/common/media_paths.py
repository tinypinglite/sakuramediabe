from pathlib import Path

from src.config.config import settings


def media_image_root_path() -> Path:
    """返回媒体图片根目录的规范绝对路径。"""
    image_root_path = Path(settings.media.import_image_root_path).expanduser()
    if not image_root_path.is_absolute():
        image_root_path = (Path.cwd() / image_root_path).resolve()
    return image_root_path
