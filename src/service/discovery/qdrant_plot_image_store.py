from collections.abc import Sequence
from functools import lru_cache

from loguru import logger
from pydantic import BaseModel

from .qdrant_thumbnail_store import QdrantThumbnailStore, models


class PlotImageVectorRecord(BaseModel):
    plot_image_id: int
    movie_id: int
    vector: list[float]


class PlotImageVectorSearchHit(BaseModel):
    plot_image_id: int
    movie_id: int
    score: float


class QdrantPlotImageStore(QdrantThumbnailStore):
    COLLECTION_NAME = "movie_plot_image_vectors_siglip2_v1"
    PAYLOAD_INDEX_FIELDS = ("movie_id",)

    def upsert_records(self, records: Sequence[PlotImageVectorRecord]) -> None:
        if not records:
            return
        self._get_client().upsert(
            collection_name=self.collection_name,
            points=[
                models.PointStruct(
                    id=int(item.plot_image_id),
                    vector=self._prepare_vector(item.vector),
                    payload={"movie_id": int(item.movie_id)},
                )
                for item in records
            ],
            wait=True,
        )

    def delete_by_plot_image_ids(self, plot_image_ids: Sequence[int]) -> None:
        self.delete_by_thumbnail_ids(plot_image_ids)

    def search(
        self,
        query_vector: Sequence[float],
        limit: int = 20,
        offset: int = 0,
        movie_ids: Sequence[int] | None = None,
        exclude_movie_ids: Sequence[int] | None = None,
    ) -> list[PlotImageVectorSearchHit]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        try:
            if not self._collection_exists():
                return []
            result = self._get_client().query_points(
                collection_name=self.collection_name,
                query=self._prepare_vector(query_vector),
                limit=int(limit),
                offset=int(offset),
                query_filter=self._build_filter(
                    movie_ids=movie_ids, exclude_movie_ids=exclude_movie_ids
                ),
                search_params=models.SearchParams(hnsw_ef=self.HNSW_EF_SEARCH),
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.warning(
                "Qdrant plot image search failed collection={} detail={}",
                self.collection_name,
                exc,
            )
            return []
        return [
            PlotImageVectorSearchHit(
                plot_image_id=int(point.id),
                movie_id=int((point.payload or {})["movie_id"]),
                score=max(0.0, min(1.0, (float(point.score or 0.0) + 1.0) / 2.0)),
            )
            for point in result.points
        ]


@lru_cache(maxsize=1)
def get_qdrant_plot_image_store() -> QdrantPlotImageStore:
    return QdrantPlotImageStore()
