"""Image 记录与物理文件的清理公共工具。

catalog 目录导入和媒体硬删除都需要这份逻辑，抽出来避免重复实现。
"""

from pathlib import Path

from src.config.config import settings
from src.model import Actor, Image, MediaThumbnail, Movie, MoviePlotImage, get_database


class ImageCleanupService:
    @staticmethod
    def image_root_path() -> Path:
        image_root_path = Path(settings.media.import_image_root_path).expanduser()
        if not image_root_path.is_absolute():
            image_root_path = (Path.cwd() / image_root_path).resolve()
        return image_root_path

    @classmethod
    def delete_image_record_if_unused(cls, image: Image | None) -> set[str]:
        if image is None:
            return set()
        if cls.image_record_is_still_used(image):
            return set()
        relative_path = image.origin
        image.delete_instance()
        return {relative_path} if relative_path else set()

    @staticmethod
    def image_record_is_still_used(image: Image) -> bool:
        database = get_database()
        return any(
            (
                database.table_exists(Movie._meta.table_name)
                and Movie.select(Movie.id).where(
                    (Movie.cover_image == image) | (Movie.thin_cover_image == image)
                ).exists(),
                database.table_exists(Actor._meta.table_name)
                and Actor.select(Actor.id).where(Actor.profile_image == image).exists(),
                database.table_exists(MoviePlotImage._meta.table_name)
                and MoviePlotImage.select(MoviePlotImage.id).where(MoviePlotImage.image == image).exists(),
                database.table_exists(MediaThumbnail._meta.table_name)
                and MediaThumbnail.select(MediaThumbnail.id).where(MediaThumbnail.image == image).exists(),
            )
        )

    @classmethod
    def delete_obsolete_image_files(cls, relative_paths: set[str]) -> None:
        if not relative_paths:
            return
        image_root = cls.image_root_path()
        for relative_path in relative_paths:
            if not relative_path:
                continue
            target_path = image_root / relative_path
            try:
                target_path.unlink()
            except FileNotFoundError:
                continue
