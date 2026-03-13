import time
from pathlib import Path

from loguru import logger

from src.common import resolve_image_file_path
from src.model import Image, Media, MediaThumbnail, Movie
from src.service.discovery.joytag_openvino_embedder import get_joytag_openvino_embedder
from src.service.discovery.lancedb_thumbnail_store import (
    LanceDbThumbnailStore,
    ThumbnailVectorRecord,
    get_lancedb_thumbnail_store,
)


class ImageSearchIndexService:
    def __init__(
        self,
        store: LanceDbThumbnailStore | None = None,
        embedder=None,
    ) -> None:
        self.store = store or get_lancedb_thumbnail_store()
        self.embedder = embedder or get_joytag_openvino_embedder()
        self._store_ready = False

    def ensure_store_ready(self) -> None:
        if self._store_ready:
            return
        vector_size = int(getattr(self.embedder, "vector_size", 0) or 0)
        if vector_size <= 0:
            raise RuntimeError("JoyTag embedder vector_size is invalid")
        self.store.ensure_table(vector_size)
        self._store_ready = True

    def index_pending_thumbnails(self) -> dict[str, int]:
        pending_ids = self._pending_thumbnail_ids()
        stats = {
            "pending_thumbnails": len(pending_ids),
            "successful_thumbnails": 0,
            "failed_thumbnails": 0,
        }
        started_at = time.time()
        if not pending_ids:
            logger.info("No pending JoyTag thumbnails for indexing")
            return stats
        logger.info(
            "Starting JoyTag thumbnail indexing pending_thumbnails={} embedder={} store={}",
            len(pending_ids),
            getattr(self.embedder, "model_name", self.embedder.__class__.__name__),
            self.store.__class__.__name__,
        )
        self.ensure_store_ready()
        for index, thumbnail_id in enumerate(pending_ids, start=1):
            logger.info(
                "Indexing JoyTag thumbnail progress={}/{} thumbnail_id={}",
                index,
                len(pending_ids),
                thumbnail_id,
            )
            if self.index_thumbnail(thumbnail_id):
                stats["successful_thumbnails"] += 1
            else:
                stats["failed_thumbnails"] += 1
        elapsed_ms = int((time.time() - started_at) * 1000)
        logger.info(
            "Finished JoyTag thumbnail indexing pending_thumbnails={} successful_thumbnails={} failed_thumbnails={} elapsed_ms={}",
            stats["pending_thumbnails"],
            stats["successful_thumbnails"],
            stats["failed_thumbnails"],
            elapsed_ms,
        )
        return stats

    @staticmethod
    def _pending_thumbnail_ids() -> list[int]:
        query = (
            MediaThumbnail.select(MediaThumbnail.id)
            .where(MediaThumbnail.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING)
            .order_by(MediaThumbnail.id.asc())
        )
        return [item.id for item in query]

    @staticmethod
    def _thumbnail_query():
        return (
            MediaThumbnail.select(MediaThumbnail, Image, Media, Movie)
            .join(Image)
            .switch(MediaThumbnail)
            .join(Media)
            .join(Movie, on=(Media.movie == Movie.movie_number))
        )

    def index_thumbnail(self, thumbnail_id: int) -> bool:
        started_at = time.time()
        thumbnail = self._thumbnail_query().where(MediaThumbnail.id == thumbnail_id).get_or_none()
        if thumbnail is None:
            logger.warning("JoyTag thumbnail not found thumbnail_id={}", thumbnail_id)
            return False
        try:
            logger.info(
                "Loading JoyTag thumbnail thumbnail_id={} media_id={} movie_id={} offset_seconds={}",
                thumbnail.id,
                thumbnail.media_id,
                thumbnail.media.movie.id,
                thumbnail.offset,
            )
            image_bytes = self._read_thumbnail_bytes(thumbnail)
            inference = self.embedder.infer_image_bytes(image_bytes)
            self.store.upsert_records(
                [
                    ThumbnailVectorRecord(
                        thumbnail_id=thumbnail.id,
                        media_id=thumbnail.media_id,
                        movie_id=thumbnail.media.movie.id,
                        offset_seconds=thumbnail.offset,
                        vector=[float(item) for item in inference.vector],
                    )
                ]
            )
            thumbnail.joytag_index_status = MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS
            thumbnail.save(only=[MediaThumbnail.joytag_index_status])
            elapsed_ms = int((time.time() - started_at) * 1000)
            logger.info(
                "Indexed JoyTag thumbnail thumbnail_id={} media_id={} movie_id={} vector_size={} elapsed_ms={}",
                thumbnail.id,
                thumbnail.media_id,
                thumbnail.media.movie.id,
                len(inference.vector),
                elapsed_ms,
            )
            return True
        except Exception as exc:
            logger.warning(
                "JoyTag thumbnail indexing failed thumbnail_id={} media_id={} movie_id={} detail={}",
                thumbnail.id,
                thumbnail.media_id,
                thumbnail.media.movie.id,
                exc,
            )
            thumbnail.joytag_index_status = MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED
            thumbnail.save(only=[MediaThumbnail.joytag_index_status])
            return False

    @staticmethod
    def _read_thumbnail_bytes(thumbnail: MediaThumbnail) -> bytes:
        image_path = resolve_image_file_path(thumbnail.image.origin)
        return Path(image_path).read_bytes()

    def delete_media_vectors(self, media_id: int) -> None:
        self.store.delete_by_media_id(media_id)

    def optimize_index(self) -> dict[str, object]:
        started_at = time.time()
        logger.info("Starting JoyTag index optimization")
        self.ensure_store_ready()
        result = self.store.optimize()
        elapsed_ms = int((time.time() - started_at) * 1000)
        result_summary = " ".join(f"{key}={value}" for key, value in result.items())
        logger.info(
            "Finished JoyTag index optimization {} elapsed_ms={}",
            result_summary,
            elapsed_ms,
        )
        return result
