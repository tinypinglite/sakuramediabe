"""One-way bridge from the exact v0.5.3 storage schema to provider refs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from peewee import Database

from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    LibraryHandle,
    ProviderOperationError,
)

LEGACY_V053_LAST_MIGRATION_NAME = "20260823_04_add_movie_plot_image_search"
LEGACY_V053_UPGRADE_MIGRATION_NAME = "20260825_01_upgrade_v053_provider_storage"


class LegacyV053UpgradeError(RuntimeError):
    """The database cannot safely be converted by the v0.5.3 bridge."""


@dataclass(frozen=True)
class LegacyV053UpgradeSummary:
    upgraded: bool
    media_count: int
    invalid_media_count: int


@dataclass(frozen=True)
class _LibraryPlan:
    library_id: int
    provider_key: str
    provider_config: dict[str, Any]
    account_key: str | None


@dataclass(frozen=True)
class _MediaPlan:
    media_id: int
    storage_ref: dict[str, Any]
    file_name: str
    file_size_bytes: int
    valid: bool


def _column_names(database: Database, table_name: str) -> set[str]:
    if table_name not in set(database.get_tables()):
        return set()
    return {column.name for column in database.get_columns(table_name)}


def _applied_migrations(database: Database) -> set[str]:
    if "schema_migration" not in set(database.get_tables()):
        return set()
    rows = database.execute_sql("SELECT name FROM schema_migration").fetchall()
    return {str(row[0]) for row in rows}


def classify_database_schema(database: Database) -> str:
    """Return fresh/current/exact legacy state; all hybrids are unsupported."""
    tables = set(database.get_tables())
    if tables <= {"schema_migration"}:
        return "fresh"
    if not {"media_library", "media"} <= tables:
        return "unsupported"

    library_columns = _column_names(database, "media_library")
    media_columns = _column_names(database, "media")
    current_library = {"provider_key", "provider_config", "account_key"}
    legacy_library = {"backend", "backend_config", "backend_account_key"}
    current_media = {"storage_ref", "file_name", "file_hash"}
    legacy_media = {"path", "backend_locator", "content_fingerprint"}

    if (
        current_library <= library_columns
        and current_media <= media_columns
        and not (legacy_library & library_columns)
        and not (legacy_media & media_columns)
    ):
        return "current"
    if (
        legacy_library <= library_columns
        and legacy_media <= media_columns
        and not (current_library & library_columns)
        and not (current_media & media_columns)
        and LEGACY_V053_LAST_MIGRATION_NAME in _applied_migrations(database)
    ):
        return "legacy_v053"
    return "unsupported"


def _json_object(value: object, *, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value) if isinstance(value, str) and value else {}
    except json.JSONDecodeError as exc:
        raise LegacyV053UpgradeError(f"invalid_json: {label}") from exc
    if not isinstance(parsed, dict):
        raise LegacyV053UpgradeError(f"invalid_json_object: {label}")
    return parsed


def _normalise_local_root(config: dict[str, Any], library_id: int) -> Path:
    value = config.get("root_path")
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise LegacyV053UpgradeError(f"invalid_local_root: library_id={library_id}")
    root = Path(value).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    return root.resolve(strict=False)


def _has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor) if path.anchor else Path()
    for index, part in enumerate(path.parts):
        if index == 0 and path.anchor and part == path.anchor:
            continue
        current = current / part
        if current.is_symlink():
            return True
    return False


def _account_uid(account_key: object, cookie: object) -> str | None:
    if isinstance(account_key, str) and account_key.strip():
        return account_key.removeprefix("cloud115:").strip() or None
    if isinstance(cookie, str):
        match = re.search(r"(?:^|;\s*)UID=([^_;\s]+)", cookie)
        if match:
            return match.group(1)
    return None


def _cloud115_provider_config(
    old_config: dict[str, Any], account_key: object, library_id: int
) -> tuple[dict[str, Any], str | None]:
    cookie = old_config.get("cookies")
    root_cid = old_config.get("root_cid")
    download_root_cid = old_config.get("download_root_cid")
    if (
        not isinstance(cookie, str)
        or not cookie
        or not isinstance(root_cid, str)
        or not root_cid
    ):
        raise LegacyV053UpgradeError(
            f"invalid_cloud115_config: library_id={library_id}"
        )
    uid = _account_uid(account_key, cookie)
    config: dict[str, Any] = {
        "web_cookie": cookie,
        "device_cookie": cookie,
        "media_root_path": "/sakuramedia",
        "downloads_root_path": "/sakuramedia_downloads",
        "media_root_cid": root_cid,
    }
    if isinstance(download_root_cid, str) and download_root_cid:
        config["downloads_root_cid"] = download_root_cid
    if uid is not None:
        config["account_uid"] = uid
    return config, uid


def _scan_cloud115_media_refs(
    provider_config: dict[str, Any], library_id: int
) -> dict[str, dict[str, Any]]:
    """Scan via the provider's public API; never import its implementation."""
    logger.info(
        "v0.5.3 upgrade cloud115 provider scan requested library_id={}",
        library_id,
    )
    handle = LibraryHandle(
        library_id=library_id,
        provider_key="cloud115",
        provider_config=provider_config,
        account_key=(
            str(provider_config["account_uid"])
            if provider_config.get("account_uid") is not None
            else None
        ),
    )
    storage = MEDIA_PROVIDER_REGISTRY.storage_for(handle)
    scanner = getattr(storage, "scan_media_refs", None)
    if not callable(scanner):
        raise LegacyV053UpgradeError(
            f"cloud115_media_ref_scan_unsupported: library_id={library_id}"
        )
    try:
        media_refs = scanner(
            source_ref={
                "version": 1,
                "kind": "cloud115_dir",
                "cid": provider_config["media_root_cid"],
            }
        )
    except ProviderOperationError as exc:
        logger.error(
            "v0.5.3 upgrade cloud115 provider scan failed "
            "library_id={} code={} retryable={}",
            library_id,
            exc.code,
            exc.retryable,
        )
        raise LegacyV053UpgradeError(
            f"cloud115_scan_failed: library_id={library_id} code={exc.code}"
        ) from exc

    refs: dict[str, dict[str, Any]] = {}
    scanned_count = 0
    skipped_count = 0
    for item in media_refs:
        scanned_count += 1
        source_ref = dict(item)
        fid = source_ref.get("fid")
        if not isinstance(fid, str) or not fid:
            skipped_count += 1
            continue
        source_ref["kind"] = "cloud115_media"
        refs[fid] = source_ref
    logger.info(
        "v0.5.3 upgrade cloud115 provider scan completed "
        "library_id={} scanned_entries={} usable_refs={} skipped_refs={}",
        library_id,
        scanned_count,
        len(refs),
        skipped_count,
    )
    return refs


def _collect_upgrade_plans(
    database: Database,
) -> tuple[list[_LibraryPlan], list[_MediaPlan]]:
    logger.info("v0.5.3 upgrade preflight started")
    orphan_count = database.execute_sql(
        "SELECT COUNT(*) FROM media WHERE library_id IS NULL"
    ).fetchone()[0]
    if orphan_count:
        logger.error(
            "v0.5.3 upgrade preflight failed orphan_media={}", orphan_count
        )
        raise LegacyV053UpgradeError(f"orphan_media: count={orphan_count}")
    logger.info("v0.5.3 upgrade preflight orphan check passed")

    library_rows = database.execute_sql(
        """
        SELECT id, backend, backend_config, backend_account_key
        FROM media_library ORDER BY id
        """
    ).fetchall()
    media_rows = database.execute_sql(
        """
        SELECT id, library_id, path, backend_locator, file_size_bytes, valid
        FROM media ORDER BY id
        """
    ).fetchall()
    logger.info(
        "v0.5.3 upgrade source rows loaded libraries={} media={}",
        len(library_rows),
        len(media_rows),
    )

    library_plans: list[_LibraryPlan] = []
    local_roots: dict[int, Path] = {}
    cloud_refs: dict[int, dict[str, dict[str, Any]]] = {}
    cloud_pickcodes: dict[int, dict[str, dict[str, Any]]] = {}

    for library_index, (
        library_id,
        backend,
        raw_config,
        backend_account_key,
    ) in enumerate(library_rows, start=1):
        logger.info(
            "v0.5.3 upgrade preparing library progress={}/{} "
            "library_id={} backend={}",
            library_index,
            len(library_rows),
            library_id,
            backend,
        )
        config = _json_object(raw_config, label=f"media_library:{library_id}")
        if backend == "local":
            root = _normalise_local_root(config, library_id)
            if _has_symlink_component(root):
                raise LegacyV053UpgradeError(
                    f"symlink_local_root_unsupported: library_id={library_id}"
                )
            local_roots[library_id] = root
            library_plans.append(
                _LibraryPlan(
                    library_id=library_id,
                    provider_key="local",
                    provider_config={
                        "media_root_path": str(root),
                        "manual_import_root_path": str(root),
                    },
                    account_key=None,
                )
            )
            logger.info(
                "v0.5.3 upgrade local library plan ready library_id={}", library_id
            )
            continue
        if backend != "cloud115":
            raise LegacyV053UpgradeError(
                f"unsupported_media_backend: library_id={library_id} backend={backend}"
            )
        provider_config, account_key = _cloud115_provider_config(
            config, backend_account_key, library_id
        )
        library_plans.append(
            _LibraryPlan(
                library_id=library_id,
                provider_key="cloud115",
                provider_config=provider_config,
                account_key=account_key,
            )
        )
        logger.info(
            "v0.5.3 upgrade cloud115 library metadata scan started library_id={}",
            library_id,
        )
        scanned = _scan_cloud115_media_refs(provider_config, library_id)
        logger.info(
            "v0.5.3 upgrade cloud115 library metadata scan finished "
            "library_id={} files={}",
            library_id,
            len(scanned),
        )
        cloud_refs[library_id] = scanned
        cloud_pickcodes[library_id] = {
            str(ref["pickcode"]): ref
            for ref in scanned.values()
            if isinstance(ref.get("pickcode"), str) and ref.get("pickcode")
        }

    backends = {plan.library_id: plan.provider_key for plan in library_plans}
    media_plans: list[_MediaPlan] = []
    invalid_media_count = 0
    newly_invalid_media_ids: list[int] = []
    local_media_count = 0
    local_unresolved_count = 0
    cloud115_media_count = 0
    cloud115_missing_count = 0
    logger.info("v0.5.3 upgrade media plan build started media={}", len(media_rows))
    for media_index, (
        media_id,
        library_id,
        raw_path,
        raw_locator,
        old_size,
        old_valid,
    ) in enumerate(media_rows, start=1):
        backend = backends.get(library_id)
        if backend == "local":
            local_media_count += 1
            root = local_roots[library_id]
            path = Path(raw_path).expanduser() if isinstance(raw_path, str) else None
            if path is not None and not path.is_absolute():
                path = Path.cwd() / path
            storage_ref: dict[str, Any] = {}
            valid = False
            file_name = path.name if path is not None else ""
            if path is not None:
                resolved_path = path.resolve(strict=False)
                try:
                    relative_path = resolved_path.relative_to(root).as_posix()
                except ValueError:
                    relative_path = ""
                if relative_path and not _has_symlink_component(path):
                    storage_ref = {
                        "version": 1,
                        "kind": "media_local_path",
                        "relative_path": relative_path,
                    }
                    valid = bool(old_valid) and path.is_file()
            media_plans.append(
                _MediaPlan(
                    media_id=media_id,
                    storage_ref=storage_ref,
                    file_name=file_name,
                    file_size_bytes=int(old_size or 0),
                    valid=valid,
                )
            )
            if not storage_ref:
                local_unresolved_count += 1
        else:
            if backend != "cloud115":
                raise LegacyV053UpgradeError(
                    f"media_library_missing: media_id={media_id} library_id={library_id}"
                )
            cloud115_media_count += 1
            locator = _json_object(
                raw_locator, label=f"media:{media_id}:backend_locator"
            )
            fid = locator.get("fid")
            pickcode = locator.get("pickcode")
            remote_ref = (
                cloud_refs[library_id].get(str(fid))
                if isinstance(fid, str) and fid
                else None
            )
            if remote_ref is None and isinstance(pickcode, str) and pickcode:
                remote_ref = cloud_pickcodes[library_id].get(pickcode)
            if remote_ref is None:
                cloud115_missing_count += 1
            storage_ref = dict(remote_ref) if remote_ref is not None else {}
            remote_name = storage_ref.get("name")
            old_name = locator.get("name")
            file_name = (
                remote_name
                if isinstance(remote_name, str)
                else old_name
                if isinstance(old_name, str)
                else ""
            )
            remote_size = storage_ref.get("size_bytes")
            media_plans.append(
                _MediaPlan(
                    media_id=media_id,
                    storage_ref=storage_ref,
                    file_name=file_name,
                    file_size_bytes=(
                        int(remote_size)
                        if isinstance(remote_size, int) and remote_size >= 0
                        else int(old_size or 0)
                    ),
                    valid=bool(old_valid) and remote_ref is not None,
                )
            )
        if not media_plans[-1].valid:
            invalid_media_count += 1
            if bool(old_valid):
                newly_invalid_media_ids.append(media_id)
        if media_index % 500 == 0 or media_index == len(media_rows):
            logger.info(
                "v0.5.3 upgrade media plan progress processed={}/{} invalid={}",
                media_index,
                len(media_rows),
                invalid_media_count,
            )
    logger.info(
        "v0.5.3 upgrade preflight completed libraries={} media={} invalid_media={} "
        "newly_invalid_media={} local_media={} local_unresolved={} "
        "cloud115_media={} cloud115_missing={}",
        len(library_plans),
        len(media_plans),
        invalid_media_count,
        len(newly_invalid_media_ids),
        local_media_count,
        local_unresolved_count,
        cloud115_media_count,
        cloud115_missing_count,
    )
    if newly_invalid_media_ids:
        logger.error(
            "v0.5.3 upgrade preflight rejected newly invalid media "
            "count={} sample_ids={}",
            len(newly_invalid_media_ids),
            newly_invalid_media_ids[:20],
        )
        raise LegacyV053UpgradeError(
            "upgrade_would_invalidate_media: "
            f"count={len(newly_invalid_media_ids)} "
            f"sample_ids={newly_invalid_media_ids[:20]}"
        )
    return library_plans, media_plans


def _apply_upgrade(
    database: Database,
    library_plans: list[_LibraryPlan],
    media_plans: list[_MediaPlan],
) -> None:
    reset_tables = (
        "image_search_session",
        "media_rapid_upload_item",
        "media_rapid_upload_batch",
        "indexer_download_client",
        "download_task",
        "download_client",
    )
    existing_tables = set(database.get_tables())
    reset_counts = {
        table_name: int(
            database.execute_sql(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        )
        for table_name in reset_tables
        if table_name in existing_tables
    }
    logger.warning(
        "v0.5.3 upgrade will reset incompatible legacy runtime tables counts={}",
        reset_counts,
    )
    logger.info(
        "v0.5.3 upgrade database transaction started libraries={} media={}",
        len(library_plans),
        len(media_plans),
    )
    with database.atomic():
        logger.info("v0.5.3 upgrade adding provider storage columns")
        database.execute_sql(
            """
            ALTER TABLE media_library
                ADD COLUMN provider_key VARCHAR(255),
                ADD COLUMN provider_config TEXT NOT NULL DEFAULT '{}',
                ADD COLUMN account_key VARCHAR(255) NULL;
            ALTER TABLE media
                ADD COLUMN storage_ref TEXT NOT NULL DEFAULT '{}',
                ADD COLUMN file_name VARCHAR(1024) NOT NULL DEFAULT '',
                ADD COLUMN file_hash VARCHAR(64) NULL
            """
        )
        logger.info(
            "v0.5.3 upgrade writing media library plans count={}", len(library_plans)
        )
        for library_index, plan in enumerate(library_plans, start=1):
            database.execute_sql(
                """
                UPDATE media_library
                SET provider_key = %s, provider_config = %s, account_key = %s
                WHERE id = %s
                """,
                (
                    plan.provider_key,
                    json.dumps(plan.provider_config, ensure_ascii=False),
                    plan.account_key,
                    plan.library_id,
                ),
            )
            logger.info(
                "v0.5.3 upgrade media library write progress={}/{} "
                "library_id={} provider={}",
                library_index,
                len(library_plans),
                plan.library_id,
                plan.provider_key,
            )
        logger.info("v0.5.3 upgrade writing media plans count={}", len(media_plans))
        for media_index, plan in enumerate(media_plans, start=1):
            database.execute_sql(
                """
                UPDATE media
                SET storage_ref = %s, file_name = %s, file_size_bytes = %s,
                    file_hash = NULL, valid = %s
                WHERE id = %s
                """,
                (
                    json.dumps(plan.storage_ref, ensure_ascii=False),
                    plan.file_name,
                    plan.file_size_bytes,
                    plan.valid,
                    plan.media_id,
                ),
            )
            if media_index % 500 == 0 or media_index == len(media_plans):
                logger.info(
                    "v0.5.3 upgrade media write progress={}/{}",
                    media_index,
                    len(media_plans),
                )

        logger.info("v0.5.3 upgrade applying indexes and media library constraint")
        database.execute_sql(
            """
            ALTER TABLE media_library ALTER COLUMN provider_key SET NOT NULL;
            CREATE INDEX media_library_provider_key ON media_library (provider_key);
            CREATE INDEX media_file_hash ON media (file_hash);

            DO $$
            DECLARE constraint_name TEXT;
            BEGIN
                FOR constraint_name IN
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = 'media'::regclass
                      AND contype = 'f'
                      AND pg_get_constraintdef(oid) LIKE 'FOREIGN KEY (library_id)%%'
                LOOP
                    EXECUTE format('ALTER TABLE media DROP CONSTRAINT %%I', constraint_name);
                END LOOP;
            END $$;
            ALTER TABLE media ALTER COLUMN library_id SET NOT NULL;
            ALTER TABLE media
                ADD CONSTRAINT media_library_id_fkey
                FOREIGN KEY (library_id) REFERENCES media_library(id) ON DELETE CASCADE
            """
        )

        thumbnail_count = int(
            database.execute_sql("SELECT COUNT(*) FROM media_thumbnail").fetchone()[0]
        )
        plot_image_count = int(
            database.execute_sql("SELECT COUNT(*) FROM movie_plot_image").fetchone()[0]
        )
        logger.info(
            "v0.5.3 upgrade resetting image search index states "
            "thumbnails={} plot_images={}",
            thumbnail_count,
            plot_image_count,
        )
        database.execute_sql(
            """
            ALTER TABLE media_thumbnail
                RENAME COLUMN joytag_index_status TO image_search_index_status;
            UPDATE media_thumbnail AS thumbnail
            SET image_search_index_status = CASE
                WHEN media.movie_number IS NULL THEN 3 ELSE 0
            END
            FROM media
            WHERE media.id = thumbnail.media_id;
            ALTER TABLE movie_plot_image
                RENAME COLUMN joytag_index_status TO image_search_index_status;
            UPDATE movie_plot_image SET image_search_index_status = 0
            """
        )
        logger.info(
            "v0.5.3 upgrade image search index states reset completed "
            "thumbnails={} plot_images={}",
            thumbnail_count,
            plot_image_count,
        )

        logger.info(
            "v0.5.3 upgrade dropping incompatible legacy runtime tables tables={}",
            reset_tables,
        )
        database.execute_sql(
            """
            DROP TABLE IF EXISTS image_search_session CASCADE;
            DROP TABLE IF EXISTS media_rapid_upload_item CASCADE;
            DROP TABLE IF EXISTS media_rapid_upload_batch CASCADE;
            DROP TABLE IF EXISTS indexer_download_client CASCADE;
            DROP TABLE IF EXISTS download_task CASCADE;
            DROP TABLE IF EXISTS download_client CASCADE
            """
        )

        logger.info("v0.5.3 upgrade dropping superseded storage columns")
        database.execute_sql(
            """
            ALTER TABLE media_library
                DROP COLUMN backend CASCADE,
                DROP COLUMN backend_config CASCADE,
                DROP COLUMN backend_account_key CASCADE;
            ALTER TABLE media
                DROP COLUMN path CASCADE,
                DROP COLUMN backend_locator CASCADE,
                DROP COLUMN storage_mode CASCADE,
                DROP COLUMN content_fingerprint CASCADE,
                DROP COLUMN thumbnail_source_fingerprint CASCADE
            """
        )
        logger.info(
            "v0.5.3 upgrade recording migration marker name={}",
            LEGACY_V053_UPGRADE_MIGRATION_NAME,
        )
        database.execute_sql(
            """
            INSERT INTO schema_migration (name, applied_at)
            VALUES (%s, CURRENT_TIMESTAMP)
            ON CONFLICT (name) DO NOTHING
            """,
            (LEGACY_V053_UPGRADE_MIGRATION_NAME,),
        )
    logger.info("v0.5.3 upgrade database transaction committed")


def upgrade_v053_database(
    database: Database, *, dry_run: bool = False
) -> LegacyV053UpgradeSummary:
    """Convert exact v0.5.3 data in place. No backup or rollback path is created."""
    logger.info("v0.5.3 upgrade database bridge started")
    state = classify_database_schema(database)
    logger.info("v0.5.3 upgrade database schema classified state={}", state)
    if state in {"current", "fresh"}:
        count = (
            int(database.execute_sql("SELECT COUNT(*) FROM media").fetchone()[0])
            if state == "current"
            else 0
        )
        invalid_count = (
            int(
                database.execute_sql(
                    "SELECT COUNT(*) FROM media WHERE valid = FALSE"
                ).fetchone()[0]
            )
            if state == "current"
            else 0
        )
        logger.info(
            "v0.5.3 upgrade database bridge skipped state={} media={} invalid_media={}",
            state,
            count,
            invalid_count,
        )
        return LegacyV053UpgradeSummary(False, count, invalid_count)
    if state != "legacy_v053":
        logger.error("v0.5.3 upgrade database bridge rejected state={}", state)
        raise LegacyV053UpgradeError(
            "unsupported_schema: only the exact v0.5.3 schema can use this bridge"
        )

    library_plans, media_plans = _collect_upgrade_plans(database)
    invalid_media_count = sum(not plan.valid for plan in media_plans)
    logger.info(
        "v0.5.3 upgrade plan ready libraries={} media={} invalid_media={}",
        len(library_plans),
        len(media_plans),
        invalid_media_count,
    )
    if dry_run:
        logger.info("v0.5.3 upgrade dry run completed; database writes skipped")
        return LegacyV053UpgradeSummary(
            upgraded=False,
            media_count=len(media_plans),
            invalid_media_count=invalid_media_count,
        )
    _apply_upgrade(database, library_plans, media_plans)
    logger.info(
        "v0.5.3 upgrade database bridge finished media={} invalid_media={}",
        len(media_plans),
        invalid_media_count,
    )
    return LegacyV053UpgradeSummary(
        upgraded=True,
        media_count=len(media_plans),
        invalid_media_count=invalid_media_count,
    )


__all__ = [
    "LEGACY_V053_UPGRADE_MIGRATION_NAME",
    "LegacyV053UpgradeError",
    "LegacyV053UpgradeSummary",
    "classify_database_schema",
    "upgrade_v053_database",
]
