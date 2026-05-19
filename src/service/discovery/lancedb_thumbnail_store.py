import inspect
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

import numpy as np
from pydantic import BaseModel
from loguru import logger

from src.config.config import settings

try:
    import lancedb
except ImportError:
    lancedb = None

try:
    import pyarrow as pa
except ImportError:
    pa = None


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


class LanceDbThumbnailStore:
    FLOAT16_ALIASES = frozenset({"float16", "halffloat"})
    SCALAR_INDEX_COLUMN_ALLOWLIST = frozenset({"movie_id", "thumbnail_id", "media_id", "offset_seconds"})

    def __init__(
        self,
        uri: str | None = None,
        table_name: str | None = None,
        db: Any | None = None,
    ) -> None:
        self.uri = uri or settings.lancedb.uri
        self.table_name = table_name or settings.lancedb.table_name
        self.distance_metric = settings.lancedb.distance_metric
        self._db = db

    @staticmethod
    def _ensure_dependency() -> None:
        if lancedb is None or pa is None:
            raise RuntimeError("lancedb is not installed. Please run `poetry install` first.")

    def _get_db(self):
        if self._db is None:
            self._ensure_dependency()
            uri = Path(self.uri).expanduser()
            uri.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(uri))
        return self._db

    def _table_exists(self) -> bool:
        db = self._get_db()
        table_names = getattr(db, "table_names", None)
        if callable(table_names):
            return self.table_name in table_names()
        try:
            db.open_table(self.table_name)
            return True
        except Exception:
            return False

    def _get_table(self):
        db = self._get_db()
        return db.open_table(self.table_name)

    @staticmethod
    def _supports_argument(callable_obj: Any, argument_name: str) -> bool:
        try:
            signature = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return True
        for parameter in signature.parameters.values():
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                return True
        return argument_name in signature.parameters

    @classmethod
    def _filter_supported_kwargs(cls, callable_obj: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            key: value
            for key, value in kwargs.items()
            if cls._supports_argument(callable_obj, key)
        }

    def ensure_table(self, vector_size: int) -> None:
        if vector_size <= 0:
            raise ValueError("vector_size must be positive")
        db = self._get_db()
        if self._table_exists():
            self._validate_vector_size(vector_size)
            return

        if pa is None:
            db.create_table(self.table_name, data=[])
            return

        schema = pa.schema(
            [
                pa.field("thumbnail_id", pa.int64()),
                pa.field("media_id", pa.int64()),
                pa.field("movie_id", pa.int64()),
                pa.field("offset_seconds", pa.int64()),
                pa.field("vector", pa.list_(pa.float16(), vector_size)),
            ]
        )
        empty_table = pa.Table.from_pylist([], schema=schema)
        db.create_table(self.table_name, data=empty_table)

    def _validate_vector_size(self, expected_size: int) -> None:
        actual_size = self._get_vector_size(self._get_table())
        if actual_size is None:
            return
        if int(actual_size) != int(expected_size):
            raise ValueError(
                f"LanceDB table `{self.table_name}` vector size mismatch: expected={expected_size}, actual={actual_size}"
            )
        actual_value_type = self._get_vector_value_type(self._get_table())
        if actual_value_type is not None and not self._is_float16_compatible_type(actual_value_type):
            raise ValueError(
                f"LanceDB table `{self.table_name}` vector dtype mismatch: expected=float16, actual={actual_value_type}"
            )

    @staticmethod
    def _get_vector_size(table: Any) -> int | None:
        schema = getattr(table, "schema", None)
        if callable(schema):
            schema = schema()
        if schema is None:
            return None
        field = getattr(schema, "field", None)
        if callable(field):
            try:
                vector_field = field("vector")
            except Exception:
                return None
            vector_type = getattr(vector_field, "type", None)
            list_size = getattr(vector_type, "list_size", None)
            if list_size is not None:
                return int(list_size)
        return None

    @staticmethod
    def _get_vector_value_type(table: Any) -> str | None:
        schema = getattr(table, "schema", None)
        if callable(schema):
            schema = schema()
        if schema is None:
            return None
        field = getattr(schema, "field", None)
        if not callable(field):
            return None
        try:
            vector_field = field("vector")
        except Exception:
            return None
        vector_type = getattr(vector_field, "type", None)
        if vector_type is None:
            return None
        value_type = getattr(vector_type, "value_type", None)
        if value_type is None:
            value_field = getattr(vector_type, "value_field", None)
            if value_field is not None:
                value_type = getattr(value_field, "type", None)
        return None if value_type is None else str(value_type)

    @classmethod
    def _is_float16_compatible_type(cls, value_type: str) -> bool:
        return str(value_type).strip().lower() in cls.FLOAT16_ALIASES

    def _list_indices(self) -> list[Any]:
        table = self._get_table()
        list_indices = getattr(table, "list_indices", None)
        if not callable(list_indices):
            return []
        try:
            indices = list_indices()
        except Exception:
            return []
        return indices if isinstance(indices, list) else list(indices)

    @staticmethod
    def _index_matches(index: Any, *, name: str | None = None, column: str | None = None) -> bool:
        if name is not None:
            index_name = getattr(index, "name", None)
            if index_name is None and isinstance(index, dict):
                index_name = index.get("name")
            if index_name == name:
                return True
        if column is None:
            return False
        for attr_name in ("columns", "column", "fields", "field_names"):
            value = getattr(index, attr_name, None)
            if value is None and isinstance(index, dict):
                value = index.get(attr_name)
            if isinstance(value, str) and value == column:
                return True
            if isinstance(value, (list, tuple, set)) and column in value:
                return True
        index_name = getattr(index, "name", None)
        if index_name is None and isinstance(index, dict):
            index_name = index.get("name")
        return isinstance(index_name, str) and column in index_name

    def _has_index(self, *, name: str | None = None, column: str | None = None) -> bool:
        return any(self._index_matches(index, name=name, column=column) for index in self._list_indices())

    @staticmethod
    def _is_existing_index_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "index" in message and "exist" in message

    def _scalar_index_columns(self) -> list[str]:
        configured_columns = getattr(settings.lancedb, "scalar_index_columns", None)
        if not configured_columns:
            return ["movie_id"]
        normalized_columns: list[str] = []
        for item in configured_columns:
            column_name = str(item).strip()
            if not column_name or column_name in normalized_columns:
                continue
            if column_name not in self.SCALAR_INDEX_COLUMN_ALLOWLIST:
                logger.warning("Unsupported scalar index column `{}` configured for LanceDB, skipping", column_name)
                continue
            normalized_columns.append(column_name)
        return normalized_columns or ["movie_id"]

    def ensure_scalar_indices(self) -> None:
        if not self._table_exists():
            return
        if self._count_rows() <= 0:
            return
        table = self._get_table()
        create_scalar_index = getattr(table, "create_scalar_index", None)
        if not callable(create_scalar_index):
            logger.warning("LanceDB table does not support create_scalar_index, skipping scalar indexes")
            return
        for column_name in self._scalar_index_columns():
            index_name = f"{column_name}_idx"
            if self._has_index(name=index_name, column=column_name):
                continue
            kwargs = self._filter_supported_kwargs(
                create_scalar_index,
                name=index_name,
            )
            try:
                create_scalar_index(column_name, **kwargs)
            except Exception as exc:
                if self._is_existing_index_error(exc):
                    continue
                raise

    def ensure_vector_index(self) -> bool:
        if not self._table_exists():
            return False
        if self._has_index(name="vector_idx", column="vector"):
            return False
        table = self._get_table()
        row_count = self._count_rows()
        if row_count <= 0:
            return False

        preferred_index_type = settings.lancedb.vector_index_type
        ivf_rq_config = None
        if preferred_index_type == "ivf_rq":
            ivf_rq_config = _build_ivf_rq_config(
                distance_type=self.distance_metric,
                num_partitions=settings.lancedb.vector_index_num_partitions,
                num_bits=settings.lancedb.vector_index_num_bits,
                max_iterations=50,
                sample_rate=256,
            )
        if ivf_rq_config is not None and self._supports_argument(table.create_index, "config"):
            try:
                kwargs = self._filter_supported_kwargs(
                    table.create_index,
                    config=ivf_rq_config,
                    name="vector_idx",
                )
                table.create_index("vector", **kwargs)
                return True
            except Exception as exc:
                logger.warning("Create IVF_RQ index failed, falling back to IVF_PQ detail={}", exc)

        kwargs = self._filter_supported_kwargs(
            table.create_index,
            metric=self.distance_metric,
            num_partitions=settings.lancedb.vector_index_num_partitions,
            num_sub_vectors=settings.lancedb.vector_index_num_sub_vectors,
            vector_column_name="vector",
            index_type="IVF_PQ",
            name="vector_idx",
        )
        try:
            table.create_index(**kwargs)
        except Exception as exc:
            if self._is_existing_index_error(exc):
                return False
            raise
        return True

    def inspect_status(self) -> dict[str, Any]:
        status = {
            "healthy": True,
            "uri": self.uri,
            "table_name": self.table_name,
            "table_exists": False,
            "row_count": None,
            "vector_size": None,
            "vector_dtype": None,
            "has_vector_index": None,
            "error": None,
        }
        try:
            table_exists = bool(self._table_exists())
            status["table_exists"] = table_exists
            if not table_exists:
                return status

            status["row_count"] = int(self._count_rows())
            table = self._get_table()
            vector_size = self._get_vector_size(table)
            status["vector_size"] = int(vector_size) if vector_size is not None else None
            status["vector_dtype"] = self._get_vector_value_type(table)
            status["has_vector_index"] = bool(self._has_index(name="vector_idx", column="vector"))
            return status
        except Exception as exc:
            status["healthy"] = False
            status["error"] = str(exc)
            return status

    def _count_rows(self) -> int:
        table = self._get_table()
        count_rows = getattr(table, "count_rows", None)
        if callable(count_rows):
            try:
                return int(count_rows())
            except TypeError:
                return int(count_rows(None))
        count = getattr(table, "count", None)
        if callable(count):
            return int(count())
        return 0

    def optimize(self) -> dict[str, Any]:
        if not self._table_exists():
            return {"compacted": False}
        if self._count_rows() <= 0:
            return {"compacted": False}
        self.ensure_scalar_indices()
        try:
            self.ensure_vector_index()
        except Exception as exc:
            logger.warning("Ensure vector index failed before optimize detail={}", exc)
        optimize = getattr(self._get_table(), "optimize", None)
        if not callable(optimize):
            return {"compacted": False}
        kwargs = self._filter_supported_kwargs(
            optimize,
            cleanup_older_than=timedelta(days=0),
            delete_unverified=False,
        )
        result = optimize(**kwargs)
        if isinstance(result, dict):
            return result
        if result is None:
            return {"compacted": True}
        return {"compacted": True, "result": result}

    @staticmethod
    def _prepare_vector(vector: Sequence[float]) -> list[np.float16]:
        return [np.float16(item) for item in vector]

    def upsert_records(self, records: Sequence[ThumbnailVectorRecord]) -> None:
        if not records:
            return
        self.ensure_table(len(records[0].vector))
        existing_row_count = self._count_rows()
        if existing_row_count > 0:
            self.delete_by_thumbnail_ids([item.thumbnail_id for item in records])
        payloads = []
        for item in records:
            payload = item.model_dump()
            payload["vector"] = self._prepare_vector(item.vector)
            payloads.append(payload)
        self._get_table().add(payloads)

    def delete_by_thumbnail_ids(self, thumbnail_ids: Sequence[int]) -> None:
        if not thumbnail_ids or not self._table_exists():
            return
        if self._count_rows() <= 0:
            return
        unique_ids = [int(item) for item in dict.fromkeys(thumbnail_ids)]
        expression = f"thumbnail_id IN ({', '.join(str(item) for item in unique_ids)})"
        self._get_table().delete(expression)

    def delete_by_media_id(self, media_id: int) -> None:
        if not self._table_exists():
            return
        if self._count_rows() <= 0:
            return
        self._get_table().delete(f"media_id = {int(media_id)}")

    @staticmethod
    def _build_filter_expression(
        movie_ids: Sequence[int] | None = None,
        exclude_movie_ids: Sequence[int] | None = None,
    ) -> str | None:
        clauses: list[str] = []
        if movie_ids:
            include_ids = [int(item) for item in dict.fromkeys(movie_ids)]
            clauses.append(f"movie_id IN ({', '.join(str(item) for item in include_ids)})")
        if exclude_movie_ids:
            excluded_ids = [int(item) for item in dict.fromkeys(exclude_movie_ids)]
            clauses.append(f"movie_id NOT IN ({', '.join(str(item) for item in excluded_ids)})")
        return " AND ".join(clauses) or None

    @staticmethod
    def _search_to_rows(search_query: Any, limit: int) -> list[dict[str, Any]]:
        limited = search_query.limit(limit)
        rows = limited.to_list()
        return rows if isinstance(rows, list) else list(rows)

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
        if not self._table_exists():
            return []
        query = self._get_table().search(list(query_vector))
        metric = getattr(query, "metric", None)
        if callable(metric):
            query = metric(self.distance_metric)
        expression = self._build_filter_expression(
            movie_ids=movie_ids,
            exclude_movie_ids=exclude_movie_ids,
        )
        if expression:
            try:
                query = query.where(expression, prefilter=True)
            except TypeError:
                query = query.where(expression)
        rows = self._search_to_rows(query, limit=limit + offset)
        selected_rows = rows[offset: offset + limit]
        return [self._parse_hit(row) for row in selected_rows]

    @staticmethod
    def _parse_hit(row: dict[str, Any]) -> ThumbnailVectorSearchHit:
        distance = float(row.get("_distance", 0.0) or 0.0)
        return ThumbnailVectorSearchHit(
            thumbnail_id=int(row["thumbnail_id"]),
            media_id=int(row["media_id"]),
            movie_id=int(row["movie_id"]),
            offset_seconds=int(row["offset_seconds"]),
            score=max(0.0, min(1.0, 1.0 - (distance / 2.0))),
        )


@lru_cache(maxsize=1)
def get_lancedb_thumbnail_store() -> LanceDbThumbnailStore:
    return LanceDbThumbnailStore()


def _build_ivf_rq_config(
    distance_type: str,
    num_partitions: int,
    num_bits: int,
    max_iterations: int,
    sample_rate: int,
) -> Any | None:
    if lancedb is None:
        return None
    index_module = getattr(lancedb, "index", None)
    if index_module is None:
        return None
    config_class = getattr(index_module, "IvfRq", None) or getattr(index_module, "IvfRQ", None)
    if config_class is None:
        return None
    kwargs = {
        "distance_type": distance_type,
        "num_partitions": num_partitions,
        "num_bits": num_bits,
        "max_iterations": max_iterations,
        "sample_rate": sample_rate,
    }
    try:
        signature = inspect.signature(config_class)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in signature.parameters
        }
    try:
        return config_class(**kwargs)
    except Exception:
        return None
