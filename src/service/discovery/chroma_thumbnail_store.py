from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel

from src.config.config import settings

try:
    import chromadb
except ImportError:
    chromadb = None


class ThumbnailVectorRecord(BaseModel):
    thumbnail_id: int
    media_id: int
    movie_id: int
    offset_seconds: int
    vector: list[float]


class ThumbnailVectorSearchHit(BaseModel):
    thumbnail_id: int
    media_id: int
    movie_id: int
    offset_seconds: int
    score: float


class ChromaThumbnailStore:
    def __init__(
        self,
        uri: str | None = None,
        collection_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.uri = uri or settings.chromadb.uri
        self.collection_name = collection_name or settings.chromadb.collection_name
        self.distance_metric = settings.chromadb.distance_metric
        self._client = client

    @staticmethod
    def _ensure_dependency() -> None:
        if chromadb is None:
            raise RuntimeError("chromadb is not installed. Please run `poetry install` first.")

    def _get_client(self):
        if self._client is None:
            self._ensure_dependency()
            uri = Path(self.uri).expanduser()
            uri.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(uri))
        return self._client

    def _collection_exists(self) -> bool:
        client = self._get_client()
        try:
            existing = client.list_collections()
        except Exception:
            return False
        for item in existing:
            name = getattr(item, "name", None)
            if name is None and isinstance(item, str):
                name = item
            if name == self.collection_name:
                return True
        return False

    def _get_collection(self):
        client = self._get_client()
        return client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": self.distance_metric},
        )

    def ensure_collection(self, vector_size: int) -> None:
        if vector_size <= 0:
            raise ValueError("vector_size must be positive")
        # Chroma 在首次 add 时根据向量自动确定维度，这里仅触发 collection 创建。
        self._get_collection()

    @staticmethod
    def _to_metadata(record: ThumbnailVectorRecord) -> dict[str, int]:
        return {
            "thumbnail_id": int(record.thumbnail_id),
            "media_id": int(record.media_id),
            "movie_id": int(record.movie_id),
            "offset_seconds": int(record.offset_seconds),
        }

    def upsert_records(self, records: Sequence[ThumbnailVectorRecord]) -> None:
        if not records:
            return
        collection = self._get_collection()
        ids = [str(int(item.thumbnail_id)) for item in records]
        embeddings = [[float(value) for value in item.vector] for item in records]
        metadatas = [self._to_metadata(item) for item in records]
        collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas)

    def delete_by_thumbnail_ids(self, thumbnail_ids: Sequence[int]) -> None:
        if not thumbnail_ids:
            return
        unique_ids = [str(int(item)) for item in dict.fromkeys(thumbnail_ids)]
        if not unique_ids:
            return
        self._get_collection().delete(ids=unique_ids)

    def delete_by_media_id(self, media_id: int) -> None:
        self._get_collection().delete(where={"media_id": int(media_id)})

    @staticmethod
    def _build_where(
        movie_ids: Sequence[int] | None,
        exclude_movie_ids: Sequence[int] | None,
    ) -> dict[str, Any] | None:
        clauses: list[dict[str, Any]] = []
        if movie_ids:
            include_ids = [int(item) for item in dict.fromkeys(movie_ids)]
            clauses.append({"movie_id": {"$in": include_ids}})
        if exclude_movie_ids:
            excluded_ids = [int(item) for item in dict.fromkeys(exclude_movie_ids)]
            clauses.append({"movie_id": {"$nin": excluded_ids}})
        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return {"$and": clauses}

    def search(
        self,
        query_vector: Sequence[float],
        limit: int = 20,
        offset: int = 0,
        movie_ids: Sequence[int] | None = None,
        exclude_movie_ids: Sequence[int] | None = None,
    ) -> list[ThumbnailVectorSearchHit]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        collection = self._get_collection()
        n_results = limit + offset
        where = self._build_where(movie_ids, exclude_movie_ids)
        query_kwargs: dict[str, Any] = {
            "query_embeddings": [[float(value) for value in query_vector]],
            "n_results": n_results,
            "include": ["metadatas", "distances"],
        }
        if where is not None:
            query_kwargs["where"] = where
        result = collection.query(**query_kwargs)
        metadatas_batch = result.get("metadatas") or []
        distances_batch = result.get("distances") or []
        if not metadatas_batch or not distances_batch:
            return []
        metadatas = metadatas_batch[0] or []
        distances = distances_batch[0] or []
        rows = list(zip(metadatas, distances))
        selected = rows[offset : offset + limit]
        return [self._parse_hit(meta, distance) for meta, distance in selected]

    @staticmethod
    def _parse_hit(metadata: dict[str, Any], distance: float | None) -> ThumbnailVectorSearchHit:
        raw_distance = float(distance if distance is not None else 0.0)
        score = max(0.0, min(1.0, 1.0 - (raw_distance / 2.0)))
        return ThumbnailVectorSearchHit(
            thumbnail_id=int(metadata["thumbnail_id"]),
            media_id=int(metadata["media_id"]),
            movie_id=int(metadata["movie_id"]),
            offset_seconds=int(metadata["offset_seconds"]),
            score=score,
        )

    def _count_rows(self) -> int:
        collection = self._get_collection()
        try:
            return int(collection.count())
        except Exception:
            return 0

    def _peek_vector_size(self) -> int | None:
        collection = self._get_collection()
        peek = getattr(collection, "peek", None)
        if not callable(peek):
            return None
        try:
            sample = peek(limit=1)
        except Exception:
            return None
        embeddings = (sample or {}).get("embeddings") or []
        if not embeddings:
            return None
        first = embeddings[0]
        if first is None:
            return None
        try:
            return int(len(first))
        except TypeError:
            return None

    def inspect_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "healthy": True,
            "uri": self.uri,
            "collection_name": self.collection_name,
            "collection_exists": False,
            "row_count": None,
            "vector_size": None,
            "error": None,
        }
        try:
            collection_exists = bool(self._collection_exists())
            status["collection_exists"] = collection_exists
            if not collection_exists:
                return status
            status["row_count"] = int(self._count_rows())
            status["vector_size"] = self._peek_vector_size()
            return status
        except Exception as exc:
            status["healthy"] = False
            status["error"] = str(exc)
            return status


@lru_cache(maxsize=1)
def get_chroma_thumbnail_store() -> ChromaThumbnailStore:
    return ChromaThumbnailStore()
