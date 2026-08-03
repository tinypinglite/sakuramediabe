from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from src.config.config import settings

try:
    from qdrant_client import QdrantClient, models
except ImportError:  # pragma: no cover - 依赖缺失时由运行期错误明确暴露。
    QdrantClient = None
    models = None


class MovieSimilarityIndexError(RuntimeError):
    """影片相似度索引不可查询。"""


class MovieSimilarityIndexNotReadyError(MovieSimilarityIndexError):
    """影片相似度索引尚未完成首次构建。"""


class MovieSimilarityUnavailableError(MovieSimilarityIndexError):
    """Qdrant 当前不可用。"""


@dataclass(frozen=True, slots=True)
class MovieSimilaritySearchHit:
    movie_id: int
    score: float


class QdrantMovieSimilarityStore:
    ALIAS_NAME = "movie_metadata_similarity"
    COLLECTION_PREFIX = "movie_metadata_similarity_v1_"
    VECTOR_NAME = "metadata"
    CLIENT_TIMEOUT_SECONDS = 30

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.url = (url or settings.qdrant.url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.qdrant.api_key
        self._client = client
        # alias 只会被原子替换、不会被删除，就绪状态单调递增，只缓存已就绪。
        self._alias_ready = False

    @staticmethod
    def _ensure_dependency() -> None:
        if QdrantClient is None or models is None:
            raise MovieSimilarityUnavailableError(
                "qdrant-client is not installed. Please run `uv sync` first."
            )

    def _get_client(self):
        if self._client is None:
            self._ensure_dependency()
            self._client = QdrantClient(
                url=self.url,
                api_key=(self.api_key or None),
                timeout=self.CLIENT_TIMEOUT_SECONDS,
            )
        return self._client

    def get_alias_target(self) -> str | None:
        try:
            aliases = self._get_client().get_aliases().aliases
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc
        for alias in aliases:
            if alias.alias_name == self.ALIAS_NAME:
                return alias.collection_name
        return None

    def is_ready(self) -> bool:
        if self._alias_ready:
            return True
        if self.get_alias_target() is None:
            return False
        self._alias_ready = True
        return True

    def list_index_collections(self) -> list[str]:
        """列出本索引前缀下的全部集合，用于清理历史遗留。"""
        try:
            collections = self._get_client().get_collections().collections
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc
        return [
            collection.name
            for collection in collections
            if collection.name.startswith(self.COLLECTION_PREFIX)
        ]

    def create_collection(self, collection_name: str) -> None:
        try:
            self._get_client().create_collection(
                collection_name=collection_name,
                vectors_config={},
                sparse_vectors_config={
                    self.VECTOR_NAME: models.SparseVectorParams(
                        # 30w 影片下倒排也只有百万级 posting，常驻内存换查询延迟。
                        index=models.SparseIndexParams(
                            on_disk=False,
                            datatype=models.Datatype.FLOAT16,
                        )
                    )
                },
                on_disk_payload=True,
            )
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc

    def upsert_sparse_points(
        self,
        collection_name: str,
        points: Sequence[tuple[int, list[int], list[float]]],
    ) -> None:
        if not points:
            return
        qdrant_points = [
            models.PointStruct(
                id=int(movie_id),
                vector={
                    self.VECTOR_NAME: models.SparseVector(
                        indices=indices,
                        values=values,
                    )
                },
            )
            for movie_id, indices, values in points
        ]
        try:
            self._get_client().upsert(
                collection_name=collection_name,
                points=qdrant_points,
                wait=True,
            )
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc

    def count(self, collection_name: str) -> int:
        try:
            return int(
                self._get_client()
                .count(collection_name=collection_name, exact=True)
                .count
            )
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc

    def activate_collection(self, collection_name: str) -> str | None:
        old_collection = self.get_alias_target()
        operations = []
        if old_collection is not None:
            operations.append(
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=self.ALIAS_NAME)
                )
            )
        operations.append(
            models.CreateAliasOperation(
                create_alias=models.CreateAlias(
                    collection_name=collection_name,
                    alias_name=self.ALIAS_NAME,
                )
            )
        )
        try:
            # 同一请求内删旧 alias、挂新 alias，查询侧不会看到半成品索引。
            self._get_client().update_collection_aliases(
                change_aliases_operations=operations
            )
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc
        self._alias_ready = True
        return old_collection

    def delete_collection(self, collection_name: str) -> None:
        try:
            self._get_client().delete_collection(collection_name=collection_name)
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc

    def search_many(
        self,
        source_movie_ids: Sequence[int],
        *,
        limit: int,
    ) -> dict[int, list[MovieSimilaritySearchHit]]:
        unique_source_ids = [int(item) for item in dict.fromkeys(source_movie_ids)]
        if not unique_source_ids or limit <= 0:
            return {movie_id: [] for movie_id in unique_source_ids}
        if not self.is_ready():
            raise MovieSimilarityIndexNotReadyError("movie similarity index is not ready")

        try:
            source_points = self._get_client().retrieve(
                collection_name=self.ALIAS_NAME,
                ids=unique_source_ids,
                with_payload=False,
                with_vectors=[self.VECTOR_NAME],
            )
            vectors_by_movie_id = {
                int(point.id): point.vector[self.VECTOR_NAME]
                for point in source_points
                if isinstance(point.vector, dict) and self.VECTOR_NAME in point.vector
            }
            searchable_ids = [
                movie_id for movie_id in unique_source_ids
                if movie_id in vectors_by_movie_id
            ]
            requests = [
                models.QueryRequest(
                    query=vectors_by_movie_id[movie_id],
                    using=self.VECTOR_NAME,
                    limit=int(limit) + 1,
                    with_payload=False,
                    with_vector=False,
                )
                for movie_id in searchable_ids
            ]
            responses = (
                self._get_client().query_batch_points(
                    collection_name=self.ALIAS_NAME,
                    requests=requests,
                )
                if requests
                else []
            )
        except MovieSimilarityIndexError:
            raise
        except Exception as exc:
            raise MovieSimilarityUnavailableError(str(exc)) from exc

        results = {movie_id: [] for movie_id in unique_source_ids}
        for source_movie_id, response in zip(searchable_ids, responses):
            hits = []
            for point in response.points:
                target_movie_id = int(point.id)
                if target_movie_id == source_movie_id:
                    continue
                hits.append(
                    MovieSimilaritySearchHit(
                        movie_id=target_movie_id,
                        score=max(0.0, min(1.0, float(point.score or 0.0))),
                    )
                )
                if len(hits) >= limit:
                    break
            results[source_movie_id] = hits
        return results


@lru_cache(maxsize=1)
def get_qdrant_movie_similarity_store() -> QdrantMovieSimilarityStore:
    return QdrantMovieSimilarityStore()
