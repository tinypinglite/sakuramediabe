#!/usr/bin/env python3
"""独立迁移脚本：将 LanceDB 缩略图向量导入 Qdrant。"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import numpy as np
except ImportError as exc:  # pragma: no cover - dependency guard for standalone usage.
    raise SystemExit("Missing dependency: numpy. Install scripts/requirements-migrate-lancedb-qdrant.txt first.") from exc

try:
    import lancedb
except ImportError as exc:  # pragma: no cover - dependency guard for standalone usage.
    raise SystemExit("Missing dependency: lancedb. Install scripts/requirements-migrate-lancedb-qdrant.txt first.") from exc

try:
    from qdrant_client import QdrantClient, models
except ImportError as exc:  # pragma: no cover - dependency guard for standalone usage.
    raise SystemExit(
        "Missing dependency: qdrant-client. Install scripts/requirements-migrate-lancedb-qdrant.txt first."
    ) from exc

try:
    from tqdm import tqdm
except ImportError as exc:  # pragma: no cover - dependency guard for standalone usage.
    raise SystemExit("Missing dependency: tqdm. Install scripts/requirements-migrate-lancedb-qdrant.txt first.") from exc


DEFAULT_LANCEDB_URI = "/data/indexes/image-search"
DEFAULT_LANCEDB_TABLE = "media_thumbnail_vectors"
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_COLLECTION = "media_thumbnail_vectors"
REQUIRED_COLUMNS = ("thumbnail_id", "media_id", "movie_id", "offset_seconds", "vector")
PAYLOAD_INDEX_FIELDS = ("movie_id", "media_id")
CLIENT_TIMEOUT_SECONDS = 30
HNSW_M = 16
HNSW_EF_CONSTRUCT = 128


@dataclass(frozen=True)
class MigrationArgs:
    lancedb_uri: str
    lancedb_table: str
    qdrant_url: str
    api_key: str | None
    collection: str
    batch_size: int
    limit: int | None
    recreate: bool
    dry_run: bool


@dataclass
class MigrationStats:
    total_source_rows: int
    scanned_rows: int = 0
    written_points: int = 0
    failed_rows: int = 0


@dataclass(frozen=True)
class LanceVectorSchema:
    size: int
    value_type: str


def parse_args(argv: Sequence[str] | None = None) -> MigrationArgs:
    parser = argparse.ArgumentParser(
        description="Migrate SakuraMedia thumbnail vectors from LanceDB to Qdrant.",
    )
    parser.add_argument("--lancedb-uri", default=DEFAULT_LANCEDB_URI, help="LanceDB 数据目录。")
    parser.add_argument("--lancedb-table", default=DEFAULT_LANCEDB_TABLE, help="LanceDB 表名。")
    parser.add_argument("--qdrant-url", default=DEFAULT_QDRANT_URL, help="Qdrant URL；测试可传 :memory:。")
    parser.add_argument("--api-key", default=None, help="Qdrant API Key。")
    parser.add_argument("--collection", default=DEFAULT_COLLECTION, help="Qdrant collection 名称。")
    parser.add_argument("--batch-size", type=int, default=100, help="每批读取并写入的向量数量。")
    parser.add_argument("--limit", type=int, default=None, help="最多迁移多少条记录，适合小批量试跑。")
    parser.add_argument("--recreate", action="store_true", help="迁移前删除并重建目标 collection。")
    parser.add_argument("--dry-run", action="store_true", help="只校验源表、目标连接和 schema，不写入数据。")
    parsed = parser.parse_args(argv)

    if parsed.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if parsed.limit is not None and parsed.limit <= 0:
        parser.error("--limit must be positive")
    return MigrationArgs(
        lancedb_uri=parsed.lancedb_uri,
        lancedb_table=parsed.lancedb_table,
        qdrant_url=parsed.qdrant_url,
        api_key=parsed.api_key,
        collection=parsed.collection,
        batch_size=parsed.batch_size,
        limit=parsed.limit,
        recreate=parsed.recreate,
        dry_run=parsed.dry_run,
    )


def connect_lancedb(uri: str):
    source_path = Path(uri).expanduser()
    if not source_path.exists():
        raise FileNotFoundError(f"LanceDB uri does not exist: {source_path}")
    return lancedb.connect(str(source_path))


def open_lancedb_table(db: Any, table_name: str):
    table_names = getattr(db, "table_names", None)
    if callable(table_names) and table_name not in table_names():
        raise RuntimeError(f"LanceDB table `{table_name}` does not exist")
    return db.open_table(table_name)


def count_lancedb_rows(table: Any) -> int:
    count_rows = getattr(table, "count_rows", None)
    if callable(count_rows):
        try:
            return int(count_rows())
        except TypeError:
            return int(count_rows(None))
    count = getattr(table, "count", None)
    if callable(count):
        return int(count())
    raise RuntimeError("LanceDB table does not support row counting")


def get_lancedb_schema(table: Any):
    schema = getattr(table, "schema", None)
    return schema() if callable(schema) else schema


def get_vector_schema(schema: Any) -> LanceVectorSchema:
    if schema is None:
        raise RuntimeError("LanceDB table schema is unavailable")
    missing_columns = [column for column in REQUIRED_COLUMNS if schema.get_field_index(column) < 0]
    if missing_columns:
        raise RuntimeError(f"LanceDB table missing required columns: {', '.join(missing_columns)}")
    vector_field = schema.field("vector")
    vector_type = getattr(vector_field, "type", None)
    list_size = getattr(vector_type, "list_size", None)
    if list_size is None:
        raise RuntimeError("LanceDB vector column must be a fixed-size list")
    value_type = getattr(vector_type, "value_type", None)
    if value_type is None:
        value_field = getattr(vector_type, "value_field", None)
        if value_field is not None:
            value_type = getattr(value_field, "type", None)
    return LanceVectorSchema(size=int(list_size), value_type=str(value_type or ""))


def is_float16_vector_schema(vector_schema: LanceVectorSchema) -> bool:
    return vector_schema.value_type.strip().lower() in {"float16", "halffloat"}


def connect_qdrant(url: str, api_key: str | None):
    if url == ":memory:":
        return QdrantClient(":memory:")
    return QdrantClient(url=url.rstrip("/"), api_key=(api_key or None), timeout=CLIENT_TIMEOUT_SECONDS)


def collection_exists(client: Any, collection_name: str) -> bool:
    exists = getattr(client, "collection_exists", None)
    if callable(exists):
        return bool(exists(collection_name))
    try:
        client.get_collection(collection_name)
        return True
    except Exception:
        return False


def get_vector_params(collection_info: Any) -> Any | None:
    params = getattr(getattr(collection_info, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    if isinstance(vectors, dict):
        return vectors.get("") or next(iter(vectors.values()), None)
    return vectors


def enum_value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value)).lower()


def validate_qdrant_collection(client: Any, collection_name: str, vector_size: int) -> None:
    info = client.get_collection(collection_name)
    vector_params = get_vector_params(info)
    if vector_params is None:
        return
    actual_size = getattr(vector_params, "size", None)
    if actual_size is not None and int(actual_size) != int(vector_size):
        raise RuntimeError(
            f"Qdrant collection `{collection_name}` vector size mismatch: "
            f"expected={vector_size}, actual={actual_size}"
        )
    actual_distance = enum_value(getattr(vector_params, "distance", None))
    if actual_distance is not None and actual_distance != "cosine":
        raise RuntimeError(
            f"Qdrant collection `{collection_name}` distance mismatch: expected=cosine, actual={actual_distance}"
        )
    actual_dtype = enum_value(getattr(vector_params, "datatype", None))
    if actual_dtype is not None and actual_dtype != "float16":
        raise RuntimeError(
            f"Qdrant collection `{collection_name}` vector dtype mismatch: expected=float16, actual={actual_dtype}"
        )


def ensure_qdrant_collection(
    client: Any,
    collection_name: str,
    vector_size: int,
    *,
    recreate: bool,
    dry_run: bool,
) -> None:
    exists = collection_exists(client, collection_name)
    if dry_run:
        if exists and not recreate:
            validate_qdrant_collection(client, collection_name, vector_size)
        action = "would recreate" if recreate and exists else "would create" if not exists else "validated"
        print(f"Qdrant collection dry-run: {action} `{collection_name}`")
        return

    if recreate and exists:
        # 显式开启 recreate 时才删除目标 collection，避免误删线上数据。
        client.delete_collection(collection_name=collection_name)
        exists = False
    if exists:
        validate_qdrant_collection(client, collection_name, vector_size)
        return

    client.create_collection(
        collection_name=collection_name,
        vectors_config=models.VectorParams(
            size=int(vector_size),
            distance=models.Distance.COSINE,
            datatype=models.Datatype.FLOAT16,
            on_disk=True,
        ),
        hnsw_config=models.HnswConfigDiff(
            m=HNSW_M,
            ef_construct=HNSW_EF_CONSTRUCT,
            on_disk=True,
        ),
        # payload 落盘，与主服务配置保持一致，减少大数据量迁移后的内存压力。
        on_disk_payload=True,
    )


def is_existing_index_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "index" in message and ("exist" in message or "already" in message)


def ensure_qdrant_payload_indices(client: Any, collection_name: str, *, dry_run: bool) -> None:
    if dry_run or not collection_exists(client, collection_name):
        return
    for field_name in PAYLOAD_INDEX_FIELDS:
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.INTEGER,
                wait=True,
            )
        except Exception as exc:
            if is_existing_index_error(exc):
                continue
            raise


def iter_arrow_table_batches(
    arrow_table: Any,
    *,
    batch_size: int,
    limit: int | None,
    offset: int = 0,
) -> Iterable[list[dict[str, Any]]]:
    remaining = limit
    sliced_table = arrow_table.slice(offset, remaining) if remaining is not None else arrow_table.slice(offset)
    for record_batch in sliced_table.to_batches(max_chunksize=batch_size):
        rows = record_batch.to_pylist()
        if remaining is not None:
            rows = rows[:remaining]
            remaining -= len(rows)
        if rows:
            yield rows
        if remaining is not None and remaining <= 0:
            return


def iter_lancedb_batches(table: Any, *, batch_size: int, limit: int | None) -> Iterable[list[dict[str, Any]]]:
    dataset = table.to_lance()
    remaining = limit
    emitted_count = 0
    try:
        scanner = dataset.scanner(columns=list(REQUIRED_COLUMNS), batch_size=batch_size)
        for record_batch in scanner.to_batches():
            rows = record_batch.to_pylist()
            if remaining is not None:
                rows = rows[:remaining]
                remaining -= len(rows)
            if rows:
                emitted_count += len(rows)
                yield rows
            if remaining is not None and remaining <= 0:
                return
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        # Lance 0.21 在部分本地 fixed_size_list<float16> 数据集上 scanner 可能触发底层 panic；回退到 Arrow 表确保迁移可完成。
        print(f"Lance scanner failed, falling back to table.to_arrow(): {exc}", file=sys.stderr)
        fallback_limit = None if limit is None else max(limit - emitted_count, 0)
        if fallback_limit == 0:
            return
        yield from iter_arrow_table_batches(
            table.to_arrow(),
            batch_size=batch_size,
            limit=fallback_limit,
            offset=emitted_count,
        )


def normalize_vector(vector: Sequence[Any], *, decode_float16_bits: bool) -> list[float]:
    if decode_float16_bits and all(isinstance(item, int) and not isinstance(item, bool) for item in vector):
        # PyArrow halffloat 在 to_pylist() 中可能表现为 uint16 位值，需要按 IEEE 754 binary16 还原。
        return np.asarray(vector, dtype=np.uint16).view(np.float16).astype(np.float32).tolist()
    return [float(item) for item in vector]


def row_to_point(row: dict[str, Any], *, decode_float16_bits: bool) -> models.PointStruct:
    # Qdrant point id 直接使用 thumbnail_id，保证重复执行时是幂等 upsert。
    thumbnail_id = int(row["thumbnail_id"])
    return models.PointStruct(
        id=thumbnail_id,
        vector=normalize_vector(row["vector"], decode_float16_bits=decode_float16_bits),
        payload={
            "media_id": int(row["media_id"]),
            "movie_id": int(row["movie_id"]),
            "offset_seconds": int(row["offset_seconds"]),
        },
    )


def migrate(args: MigrationArgs) -> MigrationStats:
    started_at = time.time()
    lancedb_table = open_lancedb_table(connect_lancedb(args.lancedb_uri), args.lancedb_table)
    total_source_rows = count_lancedb_rows(lancedb_table)
    selected_source_rows = min(total_source_rows, args.limit) if args.limit is not None else total_source_rows
    vector_schema = get_vector_schema(get_lancedb_schema(lancedb_table))
    vector_size = vector_schema.size
    decode_float16_bits = is_float16_vector_schema(vector_schema)

    qdrant_client = connect_qdrant(args.qdrant_url, args.api_key)
    ensure_qdrant_collection(
        qdrant_client,
        args.collection,
        vector_size,
        recreate=args.recreate,
        dry_run=args.dry_run,
    )
    ensure_qdrant_payload_indices(qdrant_client, args.collection, dry_run=args.dry_run)

    stats = MigrationStats(total_source_rows=total_source_rows)
    print(
        "Migration prepared: "
        f"source_rows={total_source_rows}, selected_rows={selected_source_rows}, "
        f"vector_size={vector_size}, dry_run={args.dry_run}"
    )
    if args.dry_run:
        return stats

    progress = tqdm(total=selected_source_rows, unit="point", desc="Migrating")
    try:
        for rows in iter_lancedb_batches(lancedb_table, batch_size=args.batch_size, limit=args.limit):
            points: list[models.PointStruct] = []
            for row in rows:
                try:
                    points.append(row_to_point(row, decode_float16_bits=decode_float16_bits))
                except Exception as exc:
                    stats.failed_rows += 1
                    print(f"Skip invalid row thumbnail_id={row.get('thumbnail_id')}: {exc}", file=sys.stderr)
            if points:
                qdrant_client.upsert(collection_name=args.collection, points=points, wait=True)
                stats.written_points += len(points)
            stats.scanned_rows += len(rows)
            progress.update(len(rows))
    finally:
        progress.close()

    elapsed_seconds = time.time() - started_at
    print(
        "Migration finished: "
        f"source_rows={stats.total_source_rows}, scanned_rows={stats.scanned_rows}, "
        f"written_points={stats.written_points}, failed_rows={stats.failed_rows}, "
        f"elapsed_seconds={elapsed_seconds:.2f}"
    )
    return stats


def main(argv: Sequence[str] | None = None) -> int:
    try:
        migrate(parse_args(argv))
        return 0
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
