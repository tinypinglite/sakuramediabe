import time
from collections.abc import Sequence

from loguru import logger

from src.common import resolve_image_file_path
from src.common.service_helpers import emit_progress
from src.config.config import settings
from src.model import (
    Image,
    ImageSearchSession,
    Media,
    MediaThumbnail,
    Movie,
    MoviePlotImage,
)
from src.model.base import get_database
from src.service.discovery.embedding_client import (
    EmbeddingClientError,
    get_embedding_client,
)
from src.service.discovery.image_search_index_space_service import (
    ImageSearchIndexSpaceService,
)
from src.service.discovery.qdrant_plot_image_store import (
    PlotImageVectorRecord,
    QdrantPlotImageStore,
    get_qdrant_plot_image_store,
)
from src.service.discovery.qdrant_thumbnail_store import (
    QdrantThumbnailStore,
    ThumbnailVectorRecord,
    get_qdrant_thumbnail_store,
)


class ImageSearchIndexService:
    def __init__(
        self,
        store: QdrantThumbnailStore | None = None,
        plot_store: QdrantPlotImageStore | None = None,
        embedder=None,
    ) -> None:
        self.store = store or get_qdrant_thumbnail_store()
        self.plot_store = plot_store or get_qdrant_plot_image_store()
        self.embedder = embedder or get_embedding_client()
        self._stores_ready = False

    def ensure_stores_ready(self, vector_size: int) -> None:
        if self._stores_ready:
            return
        if vector_size <= 0:
            raise RuntimeError("embedding service dimension is invalid")
        for store in (self.store, self.plot_store):
            store.ensure_table(vector_size)
            store.ensure_scalar_indices()
        self._stores_ready = True

    def index_pending_images(
        self, progress_callback=None, *, reset: bool = False
    ) -> dict[str, int]:
        stats = {
            "processed_thumbnails": 0,
            "successful_thumbnails": 0,
            "failed_thumbnails": 0,
            "processed_plot_images": 0,
            "successful_plot_images": 0,
            "failed_plot_images": 0,
        }
        started_at = time.monotonic()
        work_batch_size = max(1, int(settings.image_search.index_upsert_batch_size))
        inference_batch_size = max(1, int(settings.image_search.inference_batch_size))
        next_progress_at = 1000
        reset_stats: dict[str, int] = {}

        if reset:
            emit_progress(progress_callback, text="正在清空图像搜索索引")
            reset_stats = self._reset_for_rebuild()

        while True:
            thumbnails = self._pending_thumbnails(work_batch_size)
            plot_images = self._pending_plot_images(work_batch_size)
            if not thumbnails and not plot_images:
                break

            space = self._prepare_index_space()
            self.ensure_stores_ready(int(space.dimension))
            if thumbnails:
                successful, failed = self._index_thumbnail_batch(
                    thumbnails, inference_batch_size
                )
                stats["processed_thumbnails"] += len(thumbnails)
                stats["successful_thumbnails"] += successful
                stats["failed_thumbnails"] += failed
            if plot_images:
                successful, failed = self._index_plot_image_batch(
                    plot_images, inference_batch_size
                )
                stats["processed_plot_images"] += len(plot_images)
                stats["successful_plot_images"] += successful
                stats["failed_plot_images"] += failed

            processed = stats["processed_thumbnails"] + stats["processed_plot_images"]
            if processed >= next_progress_at:
                emit_progress(
                    progress_callback,
                    current=processed,
                    text=f"已索引 {processed} 张图片",
                    summary_patch=stats,
                )
                while next_progress_at <= processed:
                    next_progress_at += 1000

        processed = stats["processed_thumbnails"] + stats["processed_plot_images"]
        summary = {**reset_stats, **stats}
        emit_progress(
            progress_callback,
            current=processed,
            text="图像搜索索引任务完成",
            summary_patch=summary,
        )
        logger.info(
            "Finished image search indexing processed_thumbnails={} successful_thumbnails={} "
            "failed_thumbnails={} processed_plot_images={} successful_plot_images={} "
            "failed_plot_images={} elapsed_ms={}",
            stats["processed_thumbnails"],
            stats["successful_thumbnails"],
            stats["failed_thumbnails"],
            stats["processed_plot_images"],
            stats["successful_plot_images"],
            stats["failed_plot_images"],
            int((time.monotonic() - started_at) * 1000),
        )
        return summary

    def _prepare_index_space(self):
        space = self.embedder.describe()
        ImageSearchIndexSpaceService.prepare_for_indexing(space.space_id)
        return space

    def _reset_for_rebuild(self) -> dict[str, int]:
        space = self.embedder.describe()
        self.store.clear()
        self.plot_store.clear()
        self._stores_ready = False
        with get_database().atomic():
            sessions_deleted = ImageSearchSession.delete().execute()
            thumbnails_reset = (
                MediaThumbnail.update(
                    image_search_index_status=MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING
                )
                .from_(Media)
                .where(MediaThumbnail.media == Media.id, Media.movie.is_null(False))
                .execute()
            )
            plot_images_reset = MoviePlotImage.update(
                image_search_index_status=MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_PENDING
            ).execute()
            ImageSearchIndexSpaceService.set_indexed_space(space.space_id)
        return {
            "sessions_deleted": int(sessions_deleted),
            "thumbnails_reset": int(thumbnails_reset),
            "plot_images_reset": int(plot_images_reset),
        }

    @staticmethod
    def _pending_thumbnails(limit: int) -> list[MediaThumbnail]:
        # 图像检索只覆盖归属 JAV 影片的缩略图。
        return list(
            MediaThumbnail.select(MediaThumbnail, Image, Media, Movie)
            .join(Image)
            .switch(MediaThumbnail)
            .join(Media)
            .join(Movie, on=(Media.movie == Movie.movie_number))
            .where(
                MediaThumbnail.image_search_index_status
                == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING,
                Media.movie.is_null(False),
            )
            .order_by(MediaThumbnail.id.asc())
            .limit(limit)
        )

    @staticmethod
    def _pending_plot_images(limit: int) -> list[MoviePlotImage]:
        return list(
            MoviePlotImage.select(MoviePlotImage, Image, Movie)
            .join(Image)
            .switch(MoviePlotImage)
            .join(Movie)
            .where(
                MoviePlotImage.image_search_index_status
                == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_PENDING
            )
            .order_by(MoviePlotImage.id.asc())
            .limit(limit)
        )

    def _index_thumbnail_batch(
        self,
        thumbnails: Sequence[MediaThumbnail],
        inference_batch_size: int,
    ) -> tuple[int, int]:
        records: list[ThumbnailVectorRecord] = []
        failed_ids: list[int] = []
        for start in range(0, len(thumbnails), inference_batch_size):
            batch = thumbnails[start : start + inference_batch_size]
            valid_thumbnails: list[MediaThumbnail] = []
            payloads: list[bytes] = []
            for thumbnail in batch:
                try:
                    payloads.append(
                        resolve_image_file_path(thumbnail.image.origin).read_bytes()
                    )
                except FileNotFoundError:
                    logger.warning(
                        "Image search thumbnail file is missing thumbnail_id={} media_id={}",
                        thumbnail.id,
                        thumbnail.media_id,
                    )
                    failed_ids.append(thumbnail.id)
                    continue
                valid_thumbnails.append(thumbnail)

            for thumbnail, vector in zip(
                valid_thumbnails, self._embed_image_payloads(payloads)
            ):
                if vector is None:
                    logger.warning(
                        "Embedding service rejected image search thumbnail thumbnail_id={} media_id={}",
                        thumbnail.id,
                        thumbnail.media_id,
                    )
                    failed_ids.append(thumbnail.id)
                    continue
                records.append(
                    ThumbnailVectorRecord(
                        thumbnail_id=thumbnail.id,
                        media_id=thumbnail.media_id,
                        movie_id=thumbnail.media.movie.id,
                        offset_seconds=thumbnail.offset,
                        vector=[float(item) for item in vector],
                    )
                )

        if records:
            self.store.upsert_records(records)
        successful_ids = [record.thumbnail_id for record in records]
        return self._commit_statuses(
            MediaThumbnail,
            successful_ids,
            failed_ids,
            success_status=MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS,
            failed_status=MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_FAILED,
        )

    def _index_plot_image_batch(
        self,
        plot_images: Sequence[MoviePlotImage],
        inference_batch_size: int,
    ) -> tuple[int, int]:
        records: list[PlotImageVectorRecord] = []
        failed_ids: list[int] = []
        for start in range(0, len(plot_images), inference_batch_size):
            batch = plot_images[start : start + inference_batch_size]
            valid_plot_images: list[MoviePlotImage] = []
            payloads: list[bytes] = []
            for plot_image in batch:
                try:
                    payloads.append(
                        resolve_image_file_path(plot_image.image.origin).read_bytes()
                    )
                except FileNotFoundError:
                    logger.warning(
                        "Plot image file is missing plot_image_id={} movie_id={}",
                        plot_image.id,
                        plot_image.movie_id,
                    )
                    failed_ids.append(plot_image.id)
                    continue
                valid_plot_images.append(plot_image)

            for plot_image, vector in zip(
                valid_plot_images, self._embed_image_payloads(payloads)
            ):
                if vector is None:
                    logger.warning(
                        "Embedding service rejected plot image plot_image_id={} movie_id={}",
                        plot_image.id,
                        plot_image.movie_id,
                    )
                    failed_ids.append(plot_image.id)
                    continue
                records.append(
                    PlotImageVectorRecord(
                        plot_image_id=plot_image.id,
                        movie_id=plot_image.movie_id,
                        vector=[float(item) for item in vector],
                    )
                )

        if records:
            self.plot_store.upsert_records(records)
        successful_ids = [record.plot_image_id for record in records]
        return self._commit_statuses(
            MoviePlotImage,
            successful_ids,
            failed_ids,
            success_status=MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS,
            failed_status=MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_FAILED,
        )

    def _embed_image_payloads(
        self, payloads: list[bytes]
    ) -> list[Sequence[float] | None]:
        if not payloads:
            return []
        try:
            vectors = self.embedder.embed_images(payloads)
        except EmbeddingClientError as exc:
            if exc.status_code != 422:
                raise
            if len(payloads) == 1:
                return [None]
            logger.warning(
                "Embedding service rejected an image batch; retrying images individually "
                "batch_size={}",
                len(payloads),
            )
            vectors = []
            for payload in payloads:
                try:
                    item_vectors = self.embedder.embed_images([payload])
                except EmbeddingClientError as item_exc:
                    if item_exc.status_code == 422:
                        vectors.append(None)
                        continue
                    raise
                if len(item_vectors) != 1:
                    raise RuntimeError("embedding service returned invalid batch size")
                vectors.append(item_vectors[0])
            return vectors
        if len(vectors) != len(payloads):
            raise RuntimeError("embedding service returned invalid batch size")
        return vectors

    @staticmethod
    def _commit_statuses(
        model,
        successful_ids: Sequence[int],
        failed_ids: Sequence[int],
        *,
        success_status: int,
        failed_status: int,
    ) -> tuple[int, int]:
        with get_database().atomic():
            successful = ImageSearchIndexService._set_status(
                model, successful_ids, success_status
            )
            failed = ImageSearchIndexService._set_status(
                model, failed_ids, failed_status
            )
        return successful, failed

    @staticmethod
    def _set_status(model, record_ids: Sequence[int], status: int) -> int:
        normalized_ids = [int(item) for item in dict.fromkeys(record_ids)]
        if not normalized_ids:
            return 0
        return int(
            model.update(image_search_index_status=status)
            .where(model.id.in_(normalized_ids))
            .execute()
        )

    def delete_media_vectors(self, media_id: int) -> None:
        self.store.delete_by_media_id(media_id)
