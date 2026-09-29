import os
import uuid
from io import BytesIO
from pathlib import Path, PurePosixPath

from loguru import logger
from PIL import Image as PILImage

from src.common.image_store import read_image_bytes, write_pack
from src.common.media_paths import (
    MEDIA_THUMBNAILS_SUBDIR,
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
        return (
            media_image_root_path()
            / namespace
            / MOVIE_MEDIA_SUBDIR
            / str(media.id)
            / MEDIA_THUMBNAILS_SUBDIR
        )

    @staticmethod
    def thumbnail_pack_file(media: Media) -> Path:
        """缩略图包路径：与 ``thumbnails/`` 目录同级同名 + ``.zip``。"""
        thumbnails_dir = ThumbnailArtifactService.thumbnail_directory(media)
        return thumbnails_dir.with_name(f"{thumbnails_dir.name}.zip")

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

    @staticmethod
    def _cleanup_stale_temp_packs(pack_path: Path) -> None:
        # 历史崩溃可能留下未替换的临时包，重写前顺手清掉，避免长期堆积。
        for stale in pack_path.parent.glob(f"{pack_path.name}.tmp-*"):
            try:
                stale.unlink()
            except OSError:
                continue

    @classmethod
    def persist(
        cls,
        media: Media,
        artifacts: list[tuple[ThumbnailArtifact, Path]],
    ) -> int:
        if not artifacts:
            # 生成侧已保证最小数量；空输入不触碰任何文件。
            return 0

        target_dir = cls.thumbnail_directory(media)
        pack_path = cls.thumbnail_pack_file(media)
        tmp_path = pack_path.with_name(f"{pack_path.name}.tmp-{uuid.uuid4().hex}")
        backup_path = pack_path.with_name(f"{pack_path.name}.bak")
        image_root = media_image_root_path()
        initial_index_status = (
            MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
            if media.movie_number
            else MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SKIPPED
        )
        cls._cleanup_stale_temp_packs(pack_path)
        try:
            write_pack(
                tmp_path,
                [
                    (f"{artifact.offset_seconds}.webp", source)
                    for artifact, source in sorted(
                        artifacts, key=lambda item: item[0].offset_seconds
                    )
                ],
            )
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # 先原子替换包再提交数据库；提交失败时恢复旧包。
        had_pack = pack_path.exists()
        try:
            if had_pack:
                os.replace(pack_path, backup_path)
            os.replace(tmp_path, pack_path)
        except Exception:
            if had_pack and not pack_path.exists() and backup_path.exists():
                os.replace(backup_path, pack_path)
            tmp_path.unlink(missing_ok=True)
            raise

        try:
            with get_database().atomic():
                for artifact, _source in artifacts:
                    relative_path = (
                        target_dir / f"{artifact.offset_seconds}.webp"
                    ).relative_to(image_root).as_posix()
                    image = Image.create(origin=relative_path)
                    MediaThumbnail.create(
                        media=media,
                        image=image,
                        offset=artifact.offset_seconds,
                        image_search_index_status=initial_index_status,
                    )
        except Exception:
            if had_pack and backup_path.exists():
                os.replace(backup_path, pack_path)
            else:
                pack_path.unlink(missing_ok=True)
            raise

        if backup_path.exists():
            try:
                backup_path.unlink()
            except OSError as exc:
                logger.warning(
                    "Remove thumbnail pack backup failed path={} detail={}",
                    backup_path,
                    exc,
                )
        return len(artifacts)

    @staticmethod
    def read_dimensions(image_origin: str) -> tuple[int | None, int | None]:
        with PILImage.open(BytesIO(read_image_bytes(image_origin))) as image:
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
