import time
from pathlib import Path
from typing import Sequence

from loguru import logger

from src.common import resolve_image_file_path
from src.config.config import settings
from src.model import Image, Media, MediaThumbnail, Movie
from src.service.discovery.joytag_embedder_client import (
    JoyTagEmbeddingItemError,
    JoyTagEmbeddingResult,
    JoyTagInferenceClientError,
    get_joytag_embedder_client,
)
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
        self.embedder = embedder or get_joytag_embedder_client()
        self._store_ready = False

    def ensure_store_ready(self) -> None:
        if self._store_ready:
            return
        runtime = self.embedder.get_runtime_status()
        vector_size = int(getattr(runtime, "vector_size", 0) or 0)
        if vector_size <= 0:
            raise RuntimeError("JoyTag embedder vector_size is invalid")
        self.store.ensure_table(vector_size)
        self._store_ready = True

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    def index_pending_thumbnails(self, progress_callback=None) -> dict[str, int]:
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
        self._emit_progress(
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
            "Starting JoyTag thumbnail indexing pending_thumbnails={} embedder={} store={} upsert_batch_size={} optimize_every_records={} optimize_every_seconds={} optimize_on_job_end={}",
            len(pending_ids),
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
                self._emit_progress(
                    progress_callback,
                    current=current,
                    total=total,
                    text=f"正在索引缩略图 {current + 1}/{total}",
                    summary_patch=stats,
                )
                logger.info(
                    "Indexing JoyTag thumbnail batch start={} size={} total={}",
                    current + 1,
                    len(batch_ids),
                    total,
                )
                batch_records, batch_failures = self._build_vector_records_batch(batch_ids)
                if batch_failures:
                    failed_ids = [item[0] for item in batch_failures]
                    self._update_thumbnail_statuses(
                        failed_ids,
                        MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED,
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
                self._emit_progress(
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
        self._emit_progress(
            progress_callback,
            current=len(pending_ids),
            total=len(pending_ids),
            text="图像搜索索引任务完成",
            summary_patch=stats,
        )
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
                logger.warning("JoyTag thumbnail not found thumbnail_id={}", thumbnail_id)
                failures.append((thumbnail_id, "thumbnail_not_found"))
                continue
            try:
                image_payloads.append(self._read_thumbnail_bytes(thumbnail))
            except Exception as exc:
                logger.warning(
                    "JoyTag thumbnail read failed thumbnail_id={} media_id={} movie_id={} detail={}",
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
        try:
            inference_results = self.embedder.infer_image_batch(image_payloads)
        except JoyTagInferenceClientError:
            # 远端整批失败时由上层中止任务，未处理缩略图保持 PENDING。
            raise
        if len(inference_results) != len(valid_thumbnails):
            raise RuntimeError("JoyTag inference batch result count mismatch")
        records: list[tuple[int, ThumbnailVectorRecord]] = []
        for thumbnail, result in zip(valid_thumbnails, inference_results):
            if isinstance(result, JoyTagEmbeddingItemError):
                logger.warning(
                    "JoyTag thumbnail inference failed thumbnail_id={} media_id={} movie_id={} error_code={} detail={}",
                    thumbnail.id,
                    thumbnail.media_id,
                    thumbnail.media.movie.id,
                    result.error_code,
                    result.error_message,
                )
                failures.append((thumbnail.id, result.error_message))
                continue
            records.append((thumbnail.id, self._build_vector_record(thumbnail, result)))
        return records, failures

    @staticmethod
    def _build_vector_record(
        thumbnail: MediaThumbnail,
        inference: JoyTagEmbeddingResult,
    ) -> ThumbnailVectorRecord:
        logger.info(
            "Loaded JoyTag thumbnail vector thumbnail_id={} media_id={} movie_id={} offset_seconds={} vector_size={}",
            thumbnail.id,
            thumbnail.media_id,
            thumbnail.media.movie.id,
            thumbnail.offset,
            len(inference.vector),
        )
        return ThumbnailVectorRecord(
            thumbnail_id=thumbnail.id,
            media_id=thumbnail.media_id,
            movie_id=thumbnail.media.movie.id,
            offset_seconds=thumbnail.offset,
            vector=[float(item) for item in inference.vector],
        )

    @staticmethod
    def _update_thumbnail_statuses(thumbnail_ids: Sequence[int], status: int) -> int:
        normalized_ids = [int(item) for item in thumbnail_ids]
        if not normalized_ids:
            return 0
        try:
            return int(
                MediaThumbnail.update(joytag_index_status=status)
                .where(MediaThumbnail.id.in_(normalized_ids))
                .execute()
            )
        except Exception as exc:
            logger.warning(
                "Update JoyTag thumbnail status failed thumbnail_count={} status={} detail={}",
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
                "JoyTag batch vector upsert failed batch_size={} first_thumbnail_id={} last_thumbnail_id={} detail={}",
                len(records),
                thumbnail_ids[0],
                thumbnail_ids[-1],
                exc,
            )
            self._update_thumbnail_statuses(
                thumbnail_ids,
                MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED,
            )
            stats["failed_thumbnails"] += len(records)
            return 0

        updated_rows = self._update_thumbnail_statuses(
            thumbnail_ids,
            MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS,
        )
        successful_count = min(updated_rows, len(records))
        failed_count = len(records) - successful_count
        stats["successful_thumbnails"] += successful_count
        stats["failed_thumbnails"] += failed_count
        logger.info(
            "Indexed JoyTag thumbnail batch batch_size={} successful={} failed={} first_thumbnail_id={} last_thumbnail_id={}",
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
                "JoyTag segment optimize failed reason={} successful_since_last_optimize={} detail={}",
                reason,
                successful_since_last_optimize,
                exc,
            )
            return
        result_summary = " ".join(f"{key}={value}" for key, value in result.items())
        logger.info(
            "JoyTag segment optimize finished reason={} successful_since_last_optimize={} {}",
            reason,
            successful_since_last_optimize,
            result_summary,
        )

    def index_thumbnail(self, thumbnail_id: int) -> bool:
        started_at = time.time()
        thumbnail = self._thumbnail_query().where(MediaThumbnail.id == thumbnail_id).get_or_none()
        if thumbnail is None:
            logger.warning("JoyTag thumbnail not found thumbnail_id={}", thumbnail_id)
            return False
        try:
            image_bytes = self._read_thumbnail_bytes(thumbnail)
            inference = self.embedder.infer_image_bytes(image_bytes)
            record = self._build_vector_record(thumbnail, inference)
            self.store.upsert_records([record])
            thumbnail.joytag_index_status = MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS
            thumbnail.save(only=[MediaThumbnail.joytag_index_status])
            elapsed_ms = int((time.time() - started_at) * 1000)
            logger.info(
                "Indexed JoyTag thumbnail thumbnail_id={} media_id={} movie_id={} vector_size={} elapsed_ms={}",
                thumbnail.id,
                thumbnail.media_id,
                thumbnail.media.movie.id,
                len(record.vector),
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
