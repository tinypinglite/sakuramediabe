from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from loguru import logger
from pydantic import BaseModel

from src.config.config import settings

try:
    from qdrant_client import QdrantClient, models
except ImportError:  # pragma: no cover - exercised when dependency is missing at runtime.
    QdrantClient = None
    models = None


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


class QdrantThumbnailStore:
    # v0.5.3 的同名集合保存 JoyTag 向量；换名可避免把旧向量误当成 SigLIP2 数据复用。
    COLLECTION_NAME = "media_thumbnail_vectors_siglip2_v1"
    PAYLOAD_INDEX_FIELDS = ("movie_id", "media_id")
    CLIENT_TIMEOUT_SECONDS = 30
    HNSW_M = 16
    HNSW_EF_CONSTRUCT = 128
    HNSW_EF_SEARCH = 128
    # 后台 HNSW rebuild 默认会用满所有 CPU 核，这里显式限死并发，避免索引任务时把宿主机拖垮。
    MAX_OPTIMIZATION_THREADS = 1
    HNSW_MAX_INDEXING_THREADS = 2
    # 提高触发阈值降低 rebuild 频率，代价是"未索引区"变大、部分搜索走暴力扫描。
    INDEXING_THRESHOLD = 50000

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.url = (url or settings.qdrant.url).rstrip("/")
        self.collection_name = self.COLLECTION_NAME
        self.api_key = api_key if api_key is not None else settings.qdrant.api_key
        self._client = client

    @staticmethod
    def _ensure_dependency() -> None:
        if QdrantClient is None or models is None:
            raise RuntimeError("qdrant-client is not installed. Please run `uv sync` first.")

    def _get_client(self):
        if self._client is None:
            self._ensure_dependency()
            self._client = QdrantClient(
                url=self.url,
                api_key=(self.api_key or None),
                timeout=self.CLIENT_TIMEOUT_SECONDS,
            )
        return self._client

    def _collection_exists(self) -> bool:
        client = self._get_client()
        collection_exists = getattr(client, "collection_exists", None)
        if callable(collection_exists):
            return bool(collection_exists(self.collection_name))
        try:
            client.get_collection(self.collection_name)
            return True
        except Exception:
            return False

    def ensure_table(self, vector_size: int) -> None:
        if vector_size <= 0:
            raise ValueError("vector_size must be positive")
        self._ensure_dependency()
        client = self._get_client()
        if self._collection_exists():
            self._validate_collection(vector_size)
            # 历史部署可能没设线程/阈值参数，走升级路径把配置对齐到当前期望值。
            self._apply_runtime_config()
            return
        client.create_collection(
            collection_name=self.collection_name,
            vectors_config=models.VectorParams(
                size=int(vector_size),
                distance=models.Distance.COSINE,
                datatype=models.Datatype.FLOAT16,
                on_disk=True,
            ),
            hnsw_config=models.HnswConfigDiff(
                m=self.HNSW_M,
                ef_construct=self.HNSW_EF_CONSTRUCT,
                max_indexing_threads=self.HNSW_MAX_INDEXING_THREADS,
                on_disk=True,
            ),
            optimizers_config=models.OptimizersConfigDiff(
                max_optimization_threads=self.MAX_OPTIMIZATION_THREADS,
                indexing_threshold=self.INDEXING_THRESHOLD,
            ),
            # payload 也落盘，避免百万级元数据索引额外抬高常驻内存。
            on_disk_payload=True,
        )

    def _validate_collection(self, expected_size: int) -> None:
        info = self._get_client().get_collection(self.collection_name)
        vector_params = self._get_vector_params(info)
        if vector_params is None:
            return
        actual_size = getattr(vector_params, "size", None)
        if actual_size is not None and int(actual_size) != int(expected_size):
            raise ValueError(
                f"Qdrant collection `{self.collection_name}` vector size mismatch: "
                f"expected={expected_size}, actual={actual_size}"
            )
        actual_distance = getattr(vector_params, "distance", None)
        if actual_distance is not None and actual_distance != models.Distance.COSINE:
            raise ValueError(
                f"Qdrant collection `{self.collection_name}` distance mismatch: "
                f"expected=cosine, actual={actual_distance}"
            )
        actual_dtype = getattr(vector_params, "datatype", None)
        if actual_dtype is not None and actual_dtype != models.Datatype.FLOAT16:
            raise ValueError(
                f"Qdrant collection `{self.collection_name}` vector dtype mismatch: "
                f"expected=float16, actual={actual_dtype}"
            )

    def _apply_runtime_config(self) -> None:
        """把现有集合的 optimizer / hnsw 关键参数对齐到当前期望值。

        HNSW 线程/阈值仅影响 *未来* 触发的 rebuild，历史已建好的图不会被强制重建，
        因此升级本身零 CPU 冲击。任何一步失败都只 warn，不阻塞后续索引流程。
        """
        client = self._get_client()
        try:
            info = client.get_collection(self.collection_name)
        except Exception as exc:
            logger.warning(
                "Skip qdrant runtime config diff collection={} detail={}",
                self.collection_name,
                exc,
            )
            return

        config = getattr(info, "config", None)
        optimizer_cfg = getattr(config, "optimizer_config", None)
        hnsw_cfg = getattr(config, "hnsw_config", None)

        optimizers_patch: dict[str, Any] = {}
        if optimizer_cfg is not None:
            if getattr(optimizer_cfg, "max_optimization_threads", None) != self.MAX_OPTIMIZATION_THREADS:
                optimizers_patch["max_optimization_threads"] = self.MAX_OPTIMIZATION_THREADS
            if getattr(optimizer_cfg, "indexing_threshold", None) != self.INDEXING_THRESHOLD:
                optimizers_patch["indexing_threshold"] = self.INDEXING_THRESHOLD

        hnsw_patch: dict[str, Any] = {}
        if (
            hnsw_cfg is not None
            and getattr(hnsw_cfg, "max_indexing_threads", None) != self.HNSW_MAX_INDEXING_THREADS
        ):
            hnsw_patch["max_indexing_threads"] = self.HNSW_MAX_INDEXING_THREADS

        if not optimizers_patch and not hnsw_patch:
            return

        logger.info(
            "Upgrading qdrant collection config collection={} optimizers_patch={} hnsw_patch={}",
            self.collection_name,
            optimizers_patch,
            hnsw_patch,
        )
        kwargs: dict[str, Any] = {"collection_name": self.collection_name}
        if optimizers_patch:
            kwargs["optimizers_config"] = models.OptimizersConfigDiff(**optimizers_patch)
        if hnsw_patch:
            kwargs["hnsw_config"] = models.HnswConfigDiff(**hnsw_patch)
        try:
            client.update_collection(**kwargs)
        except Exception as exc:
            logger.warning(
                "Qdrant collection config upgrade failed collection={} detail={}",
                self.collection_name,
                exc,
            )

    @staticmethod
    def _get_vector_params(collection_info: Any) -> Any | None:
        params = getattr(getattr(collection_info, "config", None), "params", None)
        vectors = getattr(params, "vectors", None)
        if isinstance(vectors, dict):
            return vectors.get("") or next(iter(vectors.values()), None)
        return vectors

    @staticmethod
    def _is_existing_index_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "index" in message and ("exist" in message or "already" in message)

    def ensure_scalar_indices(self) -> None:
        if not self._collection_exists():
            return
        client = self._get_client()
        for field_name in self.PAYLOAD_INDEX_FIELDS:
            try:
                client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.INTEGER,
                    wait=True,
                )
            except Exception as exc:
                if self._is_existing_index_error(exc):
                    continue
                raise

    def ensure_vector_index(self) -> bool:
        return False

    def inspect_status(self) -> dict[str, Any]:
        status = {
            "healthy": True,
            "url": self.url,
            "collection_name": self.collection_name,
            "exists": False,
            "points_count": None,
            "vector_size": None,
            "vector_dtype": None,
            "collection_status": None,
            "error": None,
        }
        try:
            if not self._collection_exists():
                return status
            info = self._get_client().get_collection(self.collection_name)
            vector_params = self._get_vector_params(info)
            status.update(
                {
                    "exists": True,
                    "points_count": int(getattr(info, "points_count", 0) or 0),
                    "vector_size": (
                        int(vector_params.size)
                        if vector_params is not None and getattr(vector_params, "size", None) is not None
                        else None
                    ),
                    "vector_dtype": self._enum_value(getattr(vector_params, "datatype", None)) if vector_params else None,
                    "collection_status": self._enum_value(getattr(info, "status", None)),
                }
            )
            return status
        except Exception as exc:
            status["healthy"] = False
            status["error"] = str(exc)
            return status

    @staticmethod
    def _enum_value(value: Any) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "value", value))

    def optimize(self) -> dict[str, Any]:
        self.ensure_scalar_indices()
        if not self._collection_exists():
            return {"optimized": False}
        return {"optimized": True}

    @staticmethod
    def _prepare_vector(vector: Sequence[float]) -> list[float]:
        return [float(item) for item in vector]

    def upsert_records(self, records: Sequence[ThumbnailVectorRecord]) -> None:
        if not records:
            return
        self.ensure_table(len(records[0].vector))
        points = []
        for item in records:
            points.append(
                models.PointStruct(
                    id=int(item.thumbnail_id),
                    vector=self._prepare_vector(item.vector),
                    payload={
                        "media_id": int(item.media_id),
                        "movie_id": int(item.movie_id),
                        "offset_seconds": int(item.offset_seconds),
                    },
                )
            )
        self._get_client().upsert(collection_name=self.collection_name, points=points, wait=True)

    def delete_by_thumbnail_ids(self, thumbnail_ids: Sequence[int]) -> None:
        if not thumbnail_ids or not self._collection_exists():
            return
        unique_ids = [int(item) for item in dict.fromkeys(thumbnail_ids)]
        self._get_client().delete(
            collection_name=self.collection_name,
            points_selector=models.PointIdsList(points=unique_ids),
            wait=True,
        )

    def delete_by_media_id(self, media_id: int) -> None:
        if not self._collection_exists():
            return
        self._get_client().delete(
            collection_name=self.collection_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="media_id",
                            match=models.MatchValue(value=int(media_id)),
                        )
                    ]
                )
            ),
            wait=True,
        )

    def clear(self) -> None:
        if self._collection_exists():
            self._get_client().delete_collection(collection_name=self.collection_name)

    @staticmethod
    def _build_filter(
        movie_ids: Sequence[int] | None = None,
        exclude_movie_ids: Sequence[int] | None = None,
    ) -> Any | None:
        must = []
        must_not = []
        if movie_ids:
            include_ids = [int(item) for item in dict.fromkeys(movie_ids)]
            must.append(models.FieldCondition(key="movie_id", match=models.MatchAny(any=include_ids)))
        if exclude_movie_ids:
            excluded_ids = [int(item) for item in dict.fromkeys(exclude_movie_ids)]
            must_not.append(models.FieldCondition(key="movie_id", match=models.MatchAny(any=excluded_ids)))
        if not must and not must_not:
            return None
        return models.Filter(must=must or None, must_not=must_not or None)

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
        try:
            if not self._collection_exists():
                return []
            result = self._get_client().query_points(
                collection_name=self.collection_name,
                query=self._prepare_vector(query_vector),
                limit=int(limit),
                offset=int(offset),
                query_filter=self._build_filter(movie_ids=movie_ids, exclude_movie_ids=exclude_movie_ids),
                search_params=models.SearchParams(hnsw_ef=self.HNSW_EF_SEARCH),
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            logger.warning("Qdrant thumbnail search failed collection={} detail={}", self.collection_name, exc)
            return []
        return [self._parse_hit(point) for point in result.points]

    @staticmethod
    def _parse_hit(point: Any) -> ThumbnailVectorSearchHit:
        payload = point.payload or {}
        score = float(point.score or 0.0)
        return ThumbnailVectorSearchHit(
            thumbnail_id=int(point.id),
            media_id=int(payload["media_id"]),
            movie_id=int(payload["movie_id"]),
            offset_seconds=int(payload["offset_seconds"]),
            score=max(0.0, min(1.0, (score + 1.0) / 2.0)),
        )


@lru_cache(maxsize=1)
def get_qdrant_thumbnail_store() -> QdrantThumbnailStore:
    return QdrantThumbnailStore()
