import time
from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from src.common import resolve_image_file_path
from src.common.service_helpers import emit_progress
from src.config.config import settings
from src.model import Image, Media, MediaThumbnail, Movie
from src.service.discovery.embedding_client import get_embedding_client
from src.service.discovery.qdrant_thumbnail_store import (
    QdrantThumbnailStore,
    ThumbnailVectorRecord,
    get_qdrant_thumbnail_store,
)


class ImageSearchIndexService:
    def __init__(
        self,
        store: QdrantThumbnailStore | None = None,
        embedder=None,
    ) -> None:
        self.store = store or get_qdrant_thumbnail_store()
        self.embedder = embedder or get_embedding_client()
        self._store_ready = False

    def ensure_store_ready(self) -> None:
        if self._store_ready:
            return
        vector_size = int(self.embedder.describe().dimension)
        if vector_size <= 0:
            raise RuntimeError("embedding service dimension is invalid")
        self.store.ensure_table(vector_size)
        # Qdrant 过滤依赖 payload index，建表后立即确保索引存在。
        self.store.ensure_scalar_indices()
        self._store_ready = True

    def index_pending_thumbnails(self, progress_callback=None) -> dict[str, int]:
        max_records = max(1, int(settings.image_search.index_max_records_per_run))
        pending_ids = self._pending_thumbnail_ids(max_records)
        stats = {
            "pending_thumbnails": len(pending_ids),
            "successful_thumbnails": 0,
            "failed_thumbnails": 0,
        }
        started_at = time.time()
        if not pending_ids:
            logger.info("No pending image search thumbnails for indexing")
            return stats
        emit_progress(
            progress_callback,
            current=0,
            total=len(pending_ids),
            text="开始构建图像搜索索引",
            summary_patch=stats,
        )
        upsert_batch_size = max(1, int(settings.image_search.index_upsert_batch_size))
        optimize_every_records = max(1, int(settings.image_search.optimize_every_records))
        optimize_every_seconds = max(1, int(settings.image_search.optimize_every_seconds))
        optimize_on_job_end = bool(settings.image_search.optimize_on_job_end)
        logger.info(
            "Starting image search thumbnail indexing pending_thumbnails={} max_records_per_run={} embedder={} store={} upsert_batch_size={} optimize_every_records={} optimize_every_seconds={} optimize_on_job_end={}",
            len(pending_ids),
            max_records,
            getattr(self.embedder, "model_name", self.embedder.__class__.__name__),
            self.store.__class__.__name__,
            upsert_batch_size,
            optimize_every_records,
            optimize_every_seconds,
            optimize_on_job_end,
        )
        self.ensure_store_ready()
        pending_records: list[tuple[int, ThumbnailVectorRecord]] = []
        inference_batch_size = max(1, int(settings.image_search.inference_batch_size))
        successful_since_last_optimize = 0
        last_optimize_at = started_at
        try:
            for chunk_start in range(0, len(pending_ids), inference_batch_size):
                batch_ids = pending_ids[chunk_start : chunk_start + inference_batch_size]
                current = chunk_start
                total = len(pending_ids)
                emit_progress(
                    progress_callback,
                    current=current,
                    total=total,
                    text=f"正在索引缩略图 {current + 1}/{total}",
                    summary_patch=stats,
                )
                logger.info(
                    "Indexing image search thumbnail batch start={} size={} total={}",
                    current + 1,
                    len(batch_ids),
                    total,
                )
                batch_records, batch_failures = self._build_vector_records_batch(batch_ids)
                if batch_failures:
                    failed_ids = [item[0] for item in batch_failures]
                    self._update_thumbnail_statuses(
                        failed_ids,
                        MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_FAILED,
                    )
                    stats["failed_thumbnails"] += len(failed_ids)
                pending_records.extend(batch_records)
                while len(pending_records) >= upsert_batch_size:
                    flushed_count = self._flush_pending_records_batch(
                        pending_records=pending_records[:upsert_batch_size],
                        stats=stats,
                    )
                    pending_records = pending_records[upsert_batch_size:]
                    successful_since_last_optimize += flushed_count
                    now = time.time()
                    if (
                        successful_since_last_optimize >= optimize_every_records
                        or now - last_optimize_at >= optimize_every_seconds
                    ):
                        self._try_segment_optimize(
                            reason="segment",
                            successful_since_last_optimize=successful_since_last_optimize,
                        )
                        successful_since_last_optimize = 0
                        last_optimize_at = time.time()
                processed = chunk_start + len(batch_ids)
                emit_progress(
                    progress_callback,
                    current=processed,
                    total=total,
                    text=f"已完成索引 {processed}/{total}",
                    summary_patch=stats,
                )
        finally:
            if pending_records:
                flushed_count = self._flush_pending_records_batch(
                    pending_records=pending_records,
                    stats=stats,
                )
                successful_since_last_optimize += flushed_count
            if optimize_on_job_end and stats["successful_thumbnails"] > 0:
                self._try_segment_optimize(
                    reason="job_end",
                    successful_since_last_optimize=successful_since_last_optimize,
                )
        emit_progress(
            progress_callback,
            current=len(pending_ids),
            total=len(pending_ids),
            text="图像搜索索引任务完成",
            summary_patch=stats,
        )
        elapsed_ms = int((time.time() - started_at) * 1000)
        logger.info(
            "Finished image search thumbnail indexing pending_thumbnails={} successful_thumbnails={} failed_thumbnails={} elapsed_ms={}",
            stats["pending_thumbnails"],
            stats["successful_thumbnails"],
            stats["failed_thumbnails"],
            elapsed_ms,
        )
        return stats

    @staticmethod
    def _pending_thumbnail_ids(limit: int) -> list[int]:
        # 图像检索仅覆盖 JAV 影片；只挑选归属 movie 的缩略图，避免非 JAV 缩略图长期滞留待索引。
        query = (
            MediaThumbnail.select(MediaThumbnail.id)
            .join(Media)
            .where(
                MediaThumbnail.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING,
                Media.movie.is_null(False),
            )
            .order_by(MediaThumbnail.id.asc())
            .limit(limit)
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

    def _build_vector_records_batch(
        self,
        thumbnail_ids: Sequence[int],
    ) -> tuple[list[tuple[int, ThumbnailVectorRecord]], list[tuple[int, str]]]:
        failures: list[tuple[int, str]] = []
        if not thumbnail_ids:
            return [], failures
        thumbnails_by_id = {
            thumbnail.id: thumbnail
            for thumbnail in self._thumbnail_query().where(MediaThumbnail.id.in_(thumbnail_ids))
        }
        image_payloads: list[bytes] = []
        valid_thumbnails: list[MediaThumbnail] = []
        for thumbnail_id in thumbnail_ids:
            thumbnail = thumbnails_by_id.get(thumbnail_id)
            if thumbnail is None:
                logger.warning("image search thumbnail not found thumbnail_id={}", thumbnail_id)
                failures.append((thumbnail_id, "thumbnail_not_found"))
                continue
            try:
                image_payloads.append(self._read_thumbnail_bytes(thumbnail))
            except Exception as exc:
                logger.warning(
                    "image search thumbnail read failed thumbnail_id={} media_id={} movie_id={} detail={}",
                    thumbnail.id,
                    thumbnail.media_id,
                    thumbnail.media.movie.id,
                    exc,
                )
                failures.append((thumbnail.id, str(exc)))
                continue
            valid_thumbnails.append(thumbnail)
        if not valid_thumbnails:
            return [], failures
        vectors = self.embedder.embed_images(image_payloads)
        if len(vectors) != len(valid_thumbnails):
            raise RuntimeError("embedding service returned invalid batch size")
        records: list[tuple[int, ThumbnailVectorRecord]] = []
        for thumbnail, vector in zip(valid_thumbnails, vectors):
            records.append((thumbnail.id, self._build_vector_record(thumbnail, vector)))
        return records, failures

    @staticmethod
    def _build_vector_record(
        thumbnail: MediaThumbnail,
        vector: Sequence[float],
    ) -> ThumbnailVectorRecord:
        logger.info(
            "Loaded image search thumbnail vector thumbnail_id={} media_id={} movie_id={} offset_seconds={} vector_size={}",
            thumbnail.id,
            thumbnail.media_id,
            thumbnail.media.movie.id,
            thumbnail.offset,
            len(vector),
        )
        return ThumbnailVectorRecord(
            thumbnail_id=thumbnail.id,
            media_id=thumbnail.media_id,
            movie_id=thumbnail.media.movie.id,
            offset_seconds=thumbnail.offset,
            vector=[float(item) for item in vector],
        )

    @staticmethod
    def _update_thumbnail_statuses(thumbnail_ids: Sequence[int], status: int) -> int:
        normalized_ids = [int(item) for item in thumbnail_ids]
        if not normalized_ids:
            return 0
        try:
            return int(
                MediaThumbnail.update(image_search_index_status=status)
                .where(MediaThumbnail.id.in_(normalized_ids))
                .execute()
            )
        except Exception as exc:
            logger.warning(
                "Update image search thumbnail status failed thumbnail_count={} status={} detail={}",
                len(normalized_ids),
                status,
                exc,
            )
            return 0

    def _flush_pending_records_batch(
        self,
        *,
        pending_records: Sequence[tuple[int, ThumbnailVectorRecord]],
        stats: dict[str, int],
    ) -> int:
        if not pending_records:
            return 0
        thumbnail_ids = [int(item[0]) for item in pending_records]
        records = [item[1] for item in pending_records]
        try:
            self.store.upsert_records(records)
        except Exception as exc:
            logger.warning(
                "image search batch vector upsert failed batch_size={} first_thumbnail_id={} last_thumbnail_id={} detail={}",
                len(records),
                thumbnail_ids[0],
                thumbnail_ids[-1],
                exc,
            )
            self._update_thumbnail_statuses(
                thumbnail_ids,
                MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_FAILED,
            )
            stats["failed_thumbnails"] += len(records)
            return 0

        updated_rows = self._update_thumbnail_statuses(
            thumbnail_ids,
            MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS,
        )
        successful_count = min(updated_rows, len(records))
        failed_count = len(records) - successful_count
        stats["successful_thumbnails"] += successful_count
        stats["failed_thumbnails"] += failed_count
        logger.info(
            "Indexed image search thumbnail batch batch_size={} successful={} failed={} first_thumbnail_id={} last_thumbnail_id={}",
            len(records),
            successful_count,
            failed_count,
            thumbnail_ids[0],
            thumbnail_ids[-1],
        )
        return successful_count

    def _try_segment_optimize(
        self,
        *,
        reason: str,
        successful_since_last_optimize: int,
    ) -> None:
        try:
            result = self.optimize_index()
        except Exception as exc:
            logger.warning(
                "image search segment optimize failed reason={} successful_since_last_optimize={} detail={}",
                reason,
                successful_since_last_optimize,
                exc,
            )
            return
        result_summary = " ".join(f"{key}={value}" for key, value in result.items())
        logger.info(
            "image search segment optimize finished reason={} successful_since_last_optimize={} {}",
            reason,
            successful_since_last_optimize,
            result_summary,
        )

    def index_thumbnail(self, thumbnail_id: int) -> bool:
        started_at = time.time()
        thumbnail = self._thumbnail_query().where(MediaThumbnail.id == thumbnail_id).get_or_none()
        if thumbnail is None:
            logger.warning("image search thumbnail not found thumbnail_id={}", thumbnail_id)
            return False
        try:
            image_bytes = self._read_thumbnail_bytes(thumbnail)
            record = self._build_vector_record(thumbnail, self.embedder.embed_images([image_bytes])[0])
            self.store.upsert_records([record])
            thumbnail.image_search_index_status = MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
            thumbnail.save(only=[MediaThumbnail.image_search_index_status])
            elapsed_ms = int((time.time() - started_at) * 1000)
            logger.info(
                "Indexed image search thumbnail thumbnail_id={} media_id={} movie_id={} vector_size={} elapsed_ms={}",
                thumbnail.id,
                thumbnail.media_id,
                thumbnail.media.movie.id,
                len(record.vector),
                elapsed_ms,
            )
            return True
        except Exception as exc:
            logger.warning(
                "image search thumbnail indexing failed thumbnail_id={} media_id={} movie_id={} detail={}",
                thumbnail.id,
                thumbnail.media_id,
                thumbnail.media.movie.id,
                exc,
            )
            thumbnail.image_search_index_status = MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_FAILED
            thumbnail.save(only=[MediaThumbnail.image_search_index_status])
            return False

    @staticmethod
    def _read_thumbnail_bytes(thumbnail: MediaThumbnail) -> bytes:
        image_path = resolve_image_file_path(thumbnail.image.origin)
        return Path(image_path).read_bytes()

    def delete_media_vectors(self, media_id: int) -> None:
        self.store.delete_by_media_id(media_id)

    def optimize_index(self) -> dict[str, object]:
        started_at = time.time()
        logger.info("Starting image search index optimization")
        self.ensure_store_ready()
        result = self.store.optimize()
        elapsed_ms = int((time.time() - started_at) * 1000)
        result_summary = " ".join(f"{key}={value}" for key, value in result.items())
        logger.info(
            "Finished image search index optimization {} elapsed_ms={}",
            result_summary,
            elapsed_ms,
        )
        return result
