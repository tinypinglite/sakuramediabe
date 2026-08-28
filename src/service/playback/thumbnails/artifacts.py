from pathlib import Path, PurePosixPath
from shutil import move

from loguru import logger
from PIL import Image as PILImage

from src.common.media_paths import (
    MOVIE_MEDIA_SUBDIR,
    media_image_root_path,
    movie_asset_relative_dir,
    normalize_asset_dir_name,
)
from src.model import Image, Media, MediaThumbnail, get_database
from src.plugins.provider_protocol import ThumbnailArtifact
from src.schema.catalog.actors import ImageResource
from src.schema.playback.media import MediaThumbnailResource


class ThumbnailArtifactService:
    @staticmethod
    def thumbnail_directory(media: Media) -> Path:
        namespace = (
            Path(movie_asset_relative_dir(normalize_asset_dir_name(media.movie_number)))
            if media.movie_number
            else Path("videos") / str(media.video_item_id)
        )
        return media_image_root_path() / namespace / MOVIE_MEDIA_SUBDIR / str(media.id) / "thumbnails"

    @staticmethod
    def clear_directory(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for entry in directory.iterdir():
            if entry.is_file() or entry.is_symlink():
                entry.unlink()

    @staticmethod
    def _workspace_file(workspace: Path, relative_path: str) -> Path:
        normalized = (relative_path or "").strip().replace("\\", "/")
        if not normalized or normalized.startswith("/"):
            raise ValueError("thumbnail_artifact_path_invalid")
        parts = normalized.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("thumbnail_artifact_path_invalid")
        candidate = (workspace / PurePosixPath(*parts)).resolve()
        try:
            candidate.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ValueError("thumbnail_artifact_path_invalid") from exc
        return candidate

    @classmethod
    def validate_artifact(
        cls,
        workspace: Path,
        artifact: ThumbnailArtifact,
    ) -> Path:
        if artifact.offset_seconds < 0:
            raise ValueError("thumbnail_offset_invalid")
        if not artifact.relative_path.lower().endswith(".webp"):
            raise ValueError("thumbnail_artifact_not_webp")
        source = cls._workspace_file(workspace, artifact.relative_path)
        if not source.is_file() or source.stat().st_size <= 0:
            raise ValueError("thumbnail_artifact_empty")
        try:
            with PILImage.open(source) as image:
                if image.format != "WEBP":
                    raise ValueError("thumbnail_artifact_not_webp")
                image.verify()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("thumbnail_artifact_invalid") from exc
        return source

    @classmethod
    def persist(
        cls,
        media: Media,
        artifacts: list[tuple[ThumbnailArtifact, Path]],
    ) -> int:
        target_dir = cls.thumbnail_directory(media)
        cls.clear_directory(target_dir)
        image_root = media_image_root_path()
        initial_index_status = (
            MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
            if media.movie_number
            else MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SKIPPED
        )
        moved_paths: list[Path] = []
        try:
            with get_database().atomic():
                for artifact, source in artifacts:
                    target = target_dir / f"{artifact.offset_seconds}.webp"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    move(str(source), str(target))
                    moved_paths.append(target)
                    relative_path = target.relative_to(image_root).as_posix()
                    image = Image.create(
                        origin=relative_path,
                        small=relative_path,
                        medium=relative_path,
                        large=relative_path,
                    )
                    MediaThumbnail.create(
                        media=media,
                        image=image,
                        offset=artifact.offset_seconds,
                        image_search_index_status=initial_index_status,
                    )
        except Exception:
            for path in moved_paths:
                path.unlink(missing_ok=True)
            raise
        return len(moved_paths)

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
