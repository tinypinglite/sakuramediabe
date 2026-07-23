from pathlib import Path

from loguru import logger
from PIL import Image as PILImage

from src.common.media_paths import media_image_root_path
from src.model import Image, Media, MediaThumbnail, get_database
from src.schema.catalog.actors import ImageResource
from src.schema.playback.media import MediaThumbnailResource


class ThumbnailArtifactService:
    @staticmethod
    def thumbnail_directory(media: Media) -> Path:
        namespace = (
            Path("movies") / media.movie_number
            if media.movie_number
            else Path("videos") / str(media.video_item_id)
        )
        return (
            media_image_root_path()
            / namespace
            / "media"
            / media.content_fingerprint
            / "thumbnails"
        )

    @staticmethod
    def parse_offset_seconds(file_path: Path) -> int | None:
        if not file_path.stem.isdigit():
            return None
        offset = int(file_path.stem)
        return offset if offset >= 0 else None

    @classmethod
    def collect_webp_files(cls, webp_dir: Path) -> tuple[list[Path], int]:
        webp_files = list(webp_dir.glob("*.webp"))
        parseable = [
            (offset, path)
            for path in webp_files
            if (offset := cls.parse_offset_seconds(path)) is not None
        ]
        parseable.sort(key=lambda item: (item[0], item[1].name))
        return [item[1] for item in parseable], len(webp_files)

    @staticmethod
    def clear_directory(webp_dir: Path) -> None:
        webp_dir.mkdir(parents=True, exist_ok=True)
        for existing_file in webp_dir.glob("*.webp"):
            existing_file.unlink()

    @classmethod
    def persist(cls, media: Media, webp_files: list[Path]) -> int:
        created_count = 0
        image_root = media_image_root_path()
        initial_index_status = (
            MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING
            if media.movie_number
            else MediaThumbnail.JOYTAG_INDEX_STATUS_SKIPPED
        )
        with get_database().atomic():
            for webp_file in webp_files:
                offset = cls.parse_offset_seconds(webp_file)
                if offset is None:
                    logger.warning(
                        "Skipping generated thumbnail media_id={} file_name={} reason=offset_parse_failed",
                        media.id,
                        webp_file.name,
                    )
                    continue
                relative_path = webp_file.relative_to(image_root).as_posix()
                image = Image.create(
                    origin=relative_path,
                    small=relative_path,
                    medium=relative_path,
                    large=relative_path,
                )
                MediaThumbnail.create(
                    media=media,
                    image=image,
                    offset=offset,
                    joytag_index_status=initial_index_status,
                )
                created_count += 1
        return created_count

    @staticmethod
    def read_dimensions(image_origin: str) -> tuple[int | None, int | None]:
        with PILImage.open(media_image_root_path() / image_origin) as image:
            return image.size

    @classmethod
    def list_media_thumbnails(cls, media_id: int) -> list[MediaThumbnailResource]:
        thumbnails = list(
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media == media_id)
            .order_by(MediaThumbnail.offset.asc(), MediaThumbnail.id.asc())
        )
        width, height = None, None
        if thumbnails:
            try:
                width, height = cls.read_dimensions(thumbnails[0].image.origin)
            except Exception as exc:
                logger.warning(
                    "Resolve media thumbnail dimensions failed media_id={} detail={}",
                    media_id,
                    exc,
                )
        return [
            MediaThumbnailResource(
                thumbnail_id=thumbnail.id,
                media_id=thumbnail.media_id,
                offset_seconds=thumbnail.offset,
                image=ImageResource.from_attributes_model(thumbnail.image),
                width=width,
                height=height,
            )
            for thumbnail in thumbnails
        ]
