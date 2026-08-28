from collections.abc import Sequence
from pathlib import Path

from loguru import logger

from src.common import resolve_image_file_path
from src.common.service_helpers import emit_progress
from src.config.config import settings
from src.model import Image, Movie, MoviePlotImage

from .embedding_client import get_embedding_client
from .qdrant_plot_image_store import (
    PlotImageVectorRecord,
    QdrantPlotImageStore,
    get_qdrant_plot_image_store,
)


class MoviePlotImageSearchIndexService:
    def __init__(
        self, store: QdrantPlotImageStore | None = None, embedder=None
    ) -> None:
        self.store = store or get_qdrant_plot_image_store()
        self.embedder = embedder or get_embedding_client()
        self._store_ready = False

    def ensure_store_ready(self) -> None:
        if self._store_ready:
            return
        vector_size = int(self.embedder.describe().dimension)
        if vector_size <= 0:
            raise RuntimeError("embedding service dimension is invalid")
        self.store.ensure_table(vector_size)
        self.store.ensure_scalar_indices()
        self._store_ready = True

    def index_pending_plot_images(self, progress_callback=None) -> dict[str, int]:
        max_records = max(1, int(settings.image_search.index_max_records_per_run))
        pending_ids = self._pending_plot_image_ids(max_records)
        stats = {
            "pending_plot_images": len(pending_ids),
            "successful_plot_images": 0,
            "failed_plot_images": 0,
        }
        if not pending_ids:
            return stats
        self.ensure_store_ready()
        batch_size = max(1, int(settings.image_search.inference_batch_size))
        for start in range(0, len(pending_ids), batch_size):
            batch_ids = pending_ids[start : start + batch_size]
            emit_progress(
                progress_callback,
                current=start,
                total=len(pending_ids),
                text=f"正在索引剧情图 {start + 1}/{len(pending_ids)}",
                summary_patch=stats,
            )
            self._index_batch(batch_ids, stats)
        emit_progress(
            progress_callback,
            current=len(pending_ids),
            total=len(pending_ids),
            text="剧情图搜索索引任务完成",
            summary_patch=stats,
        )
        return stats

    @staticmethod
    def _pending_plot_image_ids(limit: int) -> list[int]:
        query = (
            MoviePlotImage.select(MoviePlotImage.id)
            .where(
                MoviePlotImage.image_search_index_status
                == MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_PENDING
            )
            .order_by(MoviePlotImage.id)
            .limit(limit)
        )
        return [item.id for item in query]

    def _index_batch(
        self, plot_image_ids: Sequence[int], stats: dict[str, int]
    ) -> None:
        links = {
            item.id: item
            for item in (
                MoviePlotImage.select(MoviePlotImage, Image, Movie)
                .join(Image)
                .switch(MoviePlotImage)
                .join(Movie)
                .where(MoviePlotImage.id.in_(plot_image_ids))
            )
        }
        valid_links: list[MoviePlotImage] = []
        payloads: list[bytes] = []
        failed_ids: list[int] = []
        for plot_image_id in plot_image_ids:
            link = links.get(plot_image_id)
            if link is None:
                continue
            try:
                payloads.append(
                    Path(resolve_image_file_path(link.image.origin)).read_bytes()
                )
                valid_links.append(link)
            except Exception as exc:
                logger.warning(
                    "Read plot image for search failed plot_image_id={} detail={}",
                    link.id,
                    exc,
                )
                failed_ids.append(link.id)
        if valid_links:
            vectors = self.embedder.embed_images(payloads)
            if len(vectors) != len(valid_links):
                raise RuntimeError("embedding service returned invalid batch size")
            records: list[PlotImageVectorRecord] = []
            for link, vector in zip(valid_links, vectors):
                records.append(
                    PlotImageVectorRecord(
                        plot_image_id=link.id,
                        movie_id=link.movie_id,
                        vector=[float(item) for item in vector],
                    )
                )
            if records:
                try:
                    self.store.upsert_records(records)
                except Exception as exc:
                    logger.warning(
                        "Upsert plot image vectors failed count={} detail={}",
                        len(records),
                        exc,
                    )
                    failed_ids.extend(item.plot_image_id for item in records)
                else:
                    successful_ids = [item.plot_image_id for item in records]
                    self._set_status(
                        successful_ids, MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_SUCCESS
                    )
                    stats["successful_plot_images"] += len(successful_ids)
        if failed_ids:
            self._set_status(failed_ids, MoviePlotImage.IMAGE_SEARCH_INDEX_STATUS_FAILED)
            stats["failed_plot_images"] += len(failed_ids)

    @staticmethod
    def _set_status(plot_image_ids: Sequence[int], status: int) -> None:
        if plot_image_ids:
            MoviePlotImage.update(image_search_index_status=status).where(
                MoviePlotImage.id.in_([int(item) for item in set(plot_image_ids)])
            ).execute()
