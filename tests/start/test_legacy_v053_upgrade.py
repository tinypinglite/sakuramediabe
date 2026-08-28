import json
from pathlib import Path

import pytest

from src.model import (
    DownloadClient,
    DownloadTask,
    Image,
    Indexer,
    IndexerDownloadClient,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MoviePlotImage,
    SchemaMigration,
    Subtitle,
)
from src.plugins.provider_protocol import ProviderOperationError
from src.start.legacy_v053_upgrade import (
    LEGACY_V053_QDRANT_COLLECTIONS,
    LEGACY_V053_UPGRADE_MIGRATION_NAME,
    LegacyV053UpgradeError,
    _scan_cloud115_media_refs,
    classify_database_schema,
    cleanup_legacy_v053_qdrant_collections,
    upgrade_v053_database,
)
from src.start.migrations.runner import run_pending_migrations
from tests.conftest import TEST_MODELS

PLOT_IMAGE_MIGRATION_NAME = "20260823_04_add_movie_plot_image_search"


def test_cleanup_legacy_v053_qdrant_collections_removes_only_legacy_collections(
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    class Client:
        def __init__(self, **_kwargs):
            pass

        def collection_exists(self, collection_name):
            calls.append(("exists", collection_name))
            return collection_name == "media_thumbnail_vectors"

        def delete_collection(self, *, collection_name):
            calls.append(("delete", collection_name))

    monkeypatch.setattr("qdrant_client.QdrantClient", Client)

    assert cleanup_legacy_v053_qdrant_collections() == (
        "media_thumbnail_vectors",
    )
    assert calls == [
        ("exists", "media_thumbnail_vectors"),
        ("delete", "media_thumbnail_vectors"),
        ("exists", "movie_plot_image_vectors"),
    ]
    assert LEGACY_V053_QDRANT_COLLECTIONS == (
        "media_thumbnail_vectors",
        "movie_plot_image_vectors",
    )


def test_cloud115_scan_uses_public_media_refs(
    monkeypatch,
):
    class Storage:
        def scan_media_refs(self, *, source_ref):
            assert source_ref == {
                "version": 1,
                "kind": "cloud115_dir",
                "cid": "root-cid",
            }
            return (
                {
                    "version": 1,
                    "kind": "cloud115_media",
                    "fid": "fid-1",
                    "parent_cid": "parent",
                    "pickcode": "pickcode",
                    "name": "movie.mp4",
                    "size_bytes": 10,
                    "sha1": "ABC",
                    "is_dir": False,
                },
            )

    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.MEDIA_PROVIDER_REGISTRY.storage_for",
        lambda _handle: Storage(),
    )

    refs = _scan_cloud115_media_refs(
        {
            "device_cookie": "cookie",
            "media_root_cid": "root-cid",
            "account_uid": "123",
        },
        8,
    )

    assert refs["fid-1"]["kind"] == "cloud115_media"
    assert refs["fid-1"]["parent_cid"] == "parent"


def test_cloud115_missing_root_aborts_upgrade_scan(monkeypatch):
    class Storage:
        def scan_media_refs(self, *, source_ref):
            raise ProviderOperationError(
                provider_key="cloud115",
                operation="scan_media_refs",
                code="source_not_found",
                safe_message="missing",
                retryable=False,
            )

    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.MEDIA_PROVIDER_REGISTRY.storage_for",
        lambda _handle: Storage(),
    )

    with pytest.raises(
        LegacyV053UpgradeError,
        match="cloud115_scan_failed: library_id=8 code=source_not_found",
    ):
        _scan_cloud115_media_refs(
            {"device_cookie": "cookie", "media_root_cid": "missing-root"}, 8
        )


def _column_names(database, table_name: str) -> set[str]:
    return {column.name for column in database.get_columns(table_name)}


def _create_v053_schema(database) -> None:
    database.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    database.create_tables(TEST_MODELS)

    database.execute_sql(
        """
        ALTER TABLE media_library
            ADD COLUMN backend VARCHAR(32) NOT NULL DEFAULT 'local',
            ADD COLUMN backend_config TEXT NOT NULL DEFAULT '{}',
            ADD COLUMN backend_account_key VARCHAR(255) NULL;
        CREATE UNIQUE INDEX media_library_backend_account_key
            ON media_library (backend_account_key);
        ALTER TABLE media_library
            DROP COLUMN provider_key CASCADE,
            DROP COLUMN provider_config CASCADE,
            DROP COLUMN account_key CASCADE
        """
    )
    database.execute_sql(
        """
        ALTER TABLE media
            ALTER COLUMN library_id DROP NOT NULL,
            ADD COLUMN path VARCHAR(1024) NULL,
            ADD COLUMN backend_locator TEXT NULL,
            ADD COLUMN storage_mode VARCHAR(32) NULL,
            ADD COLUMN content_fingerprint VARCHAR(255) NULL,
            ADD COLUMN special_tags VARCHAR(255) NOT NULL DEFAULT '普通',
            ADD COLUMN thumbnail_source_fingerprint VARCHAR(255) NULL;
        CREATE UNIQUE INDEX media_path ON media (path);
        CREATE INDEX media_content_fingerprint ON media (content_fingerprint);
        ALTER TABLE media
            DROP COLUMN storage_ref CASCADE,
            DROP COLUMN file_name CASCADE,
            DROP COLUMN file_hash CASCADE
        """
    )
    database.execute_sql(
        "ALTER TABLE media_thumbnail RENAME COLUMN image_search_index_status "
        "TO joytag_index_status"
    )
    database.execute_sql(
        "ALTER TABLE movie_plot_image RENAME COLUMN image_search_index_status "
        "TO joytag_index_status"
    )

    for table_name in ("download_task", "indexer_download_client", "download_client"):
        database.execute_sql(f'DROP TABLE "{table_name}" CASCADE')
    database.execute_sql(
        """
        CREATE TABLE download_client (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            media_library_id INTEGER NOT NULL REFERENCES media_library(id) ON DELETE CASCADE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE indexer_download_client (
            id SERIAL PRIMARY KEY,
            indexer_id INTEGER NOT NULL REFERENCES indexer(id) ON DELETE CASCADE,
            download_client_id INTEGER NOT NULL REFERENCES download_client(id) ON DELETE CASCADE,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE download_task (
            id SERIAL PRIMARY KEY,
            client_id INTEGER NOT NULL REFERENCES download_client(id) ON DELETE CASCADE,
            movie_number VARCHAR(255) NULL,
            name VARCHAR(255) NOT NULL,
            info_hash VARCHAR(128) NOT NULL,
            save_path VARCHAR(1024) NOT NULL,
            progress DOUBLE PRECISION NOT NULL DEFAULT 0,
            download_state VARCHAR(32) NOT NULL DEFAULT 'downloading',
            import_status VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE media_rapid_upload_batch (
            id SERIAL PRIMARY KEY,
            target_library_id INTEGER NOT NULL REFERENCES media_library(id),
            state VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE media_rapid_upload_item (
            id SERIAL PRIMARY KEY,
            batch_id INTEGER NOT NULL REFERENCES media_rapid_upload_batch(id) ON DELETE CASCADE,
            media_id INTEGER NULL REFERENCES media(id) ON DELETE SET NULL,
            source_path VARCHAR(1024) NOT NULL,
            state VARCHAR(32) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    for name in (
        "20260821_01_consolidate_task_runtime",
        "20260823_01_unify_movie_collection_owner",
        "20260823_02_backfill_actor_gender_from_movie_extra",
        "20260823_03_add_movie_blacklist",
        PLOT_IMAGE_MIGRATION_NAME,
    ):
        SchemaMigration.create(name=name)


def _insert_v053_data(database, tmp_path: Path) -> dict[str, int]:
    image = Image.create(
        origin="covers/ABP-001.webp",
        small="covers/ABP-001-small.webp",
        medium="covers/ABP-001-medium.webp",
        large="covers/ABP-001-large.webp",
    )
    movie = Movie.create(
        movie_number="ABP-001",
        javdb_id="javdb-1",
        title="ABP-001",
        cover_image=image,
    )
    plot_image = Image.create(
        origin="plots/ABP-001-1.webp",
        small="plots/ABP-001-1-small.webp",
        medium="plots/ABP-001-1-medium.webp",
        large="plots/ABP-001-1-large.webp",
    )
    plot_link_id = database.execute_sql(
        """
        INSERT INTO movie_plot_image (movie_id, image_id, joytag_index_status)
        VALUES (%s, %s, 2)
        RETURNING id
        """,
        (movie.id, plot_image.id),
    ).fetchone()[0]
    subtitle = Subtitle.create(movie=movie, file_path="ABP-001/ABP-001.srt")

    local_root = tmp_path / "media"
    local_file = local_root / "jav" / "ABP-001.mp4"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"local-media")

    local_library_id = database.execute_sql(
        """
        INSERT INTO media_library (name, backend, backend_config, created_at, updated_at)
        VALUES (%s, 'local', %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id
        """,
        ("本地库", json.dumps({"root_path": str(local_root)})),
    ).fetchone()[0]
    cloud_library_id = database.execute_sql(
        """
        INSERT INTO media_library (
            name, backend, backend_config, backend_account_key, created_at, updated_at
        ) VALUES (%s, 'cloud115', %s, 'cloud115:12345', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id
        """,
        (
            "115库",
            json.dumps(
                {
                    "cookies": "UID=12345_A1_1; CID=c; SEID=s",
                    "root_cid": "root-cid",
                    "download_root_cid": "downloads-cid",
                    "app": "alipaymini",
                }
            ),
        ),
    ).fetchone()[0]

    def insert_media(library_id: int, *, path=None, locator=None, size=10) -> int:
        return database.execute_sql(
            """
            INSERT INTO media (
                movie_number, library_id, path, backend_locator, content_fingerprint,
                file_size_bytes, duration_seconds, valid, thumbnail_generation_state,
                thumbnail_attempt_count, thumbnail_deferred_count, created_at, updated_at
            ) VALUES (
                'ABP-001', %s, %s, %s, 'sha1:OLD', %s, 120, TRUE, 'succeeded',
                0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            ) RETURNING id
            """,
            (
                library_id,
                path,
                json.dumps(locator) if locator is not None else None,
                size,
            ),
        ).fetchone()[0]

    local_media_id = insert_media(
        local_library_id, path=str(local_file), size=len(b"local-media")
    )
    cloud_media_id = insert_media(
        cloud_library_id,
        locator={
            "fid": "found-fid",
            "pickcode": "found-pickcode",
            "name": "ABP-001.mp4",
            "source_path": "/sakuramedia/jav/ABP-001.mp4",
        },
    )
    missing_media_id = insert_media(
        cloud_library_id,
        locator={
            "fid": "missing-fid",
            "pickcode": "missing-pickcode",
            "name": "missing.mp4",
            "source_path": "/sakuramedia/jav/missing.mp4",
        },
    )

    thumbnail_id = database.execute_sql(
        """
        INSERT INTO media_thumbnail (
            media_id, image_id, "offset", joytag_index_status, created_at, updated_at
        ) VALUES (%s, %s, 30, 2, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id
        """,
        (cloud_media_id, plot_image.id),
    ).fetchone()[0]
    database.execute_sql(
        """
        INSERT INTO media_progress (
            media_id, position_seconds, created_at, updated_at
        ) VALUES (%s, 45, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (cloud_media_id,),
    )
    database.execute_sql(
        """
        INSERT INTO media_point (
            media_id, thumbnail_id, offset_seconds, created_at, updated_at
        ) VALUES (%s, %s, 30, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (cloud_media_id, thumbnail_id),
    )
    database.execute_sql(
        """
        INSERT INTO media_clip (
            media_id, movie_number, start_offset_seconds, end_offset_seconds,
            title, file_path, file_size_bytes, duration_seconds, created_at, updated_at
        ) VALUES (%s, 'ABP-001', 10, 20, 'clip', 'ABP-001/clip.mp4',
                  0, 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (cloud_media_id,),
    )
    indexer = Indexer.create(name="保留索引器", url="https://example.test", kind="bt")
    client_id = database.execute_sql(
        """
        INSERT INTO download_client (
            name, kind, media_library_id, created_at, updated_at
        ) VALUES ('旧下载器', 'cloud115', %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        RETURNING id
        """,
        (cloud_library_id,),
    ).fetchone()[0]
    database.execute_sql(
        """
        INSERT INTO indexer_download_client (
            indexer_id, download_client_id, created_at, updated_at
        ) VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (indexer.id, client_id),
    )
    database.execute_sql(
        """
        INSERT INTO download_task (
            client_id, movie_number, name, info_hash, save_path, created_at, updated_at
        ) VALUES (%s, 'ABP-001', '旧任务', 'hash', '/downloads',
                  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (client_id,),
    )
    return {
        "local_library_id": local_library_id,
        "cloud_library_id": cloud_library_id,
        "local_media_id": local_media_id,
        "cloud_media_id": cloud_media_id,
        "missing_media_id": missing_media_id,
        "thumbnail_id": thumbnail_id,
        "plot_link_id": plot_link_id,
        "subtitle_id": subtitle.id,
        "indexer_id": indexer.id,
    }


def test_v053_upgrade_preserves_media_memory(
    clean_db, tmp_path, monkeypatch
):
    _create_v053_schema(clean_db)
    ids = _insert_v053_data(clean_db, tmp_path)

    def fake_scan(_provider_config, _library_id):
        return {
            "found-fid": {
                "version": 1,
                "kind": "cloud115_media",
                "fid": "found-fid",
                "parent_cid": "parent-cid",
                "pickcode": "found-pickcode",
                "name": "ABP-001-renamed.mp4",
                "size_bytes": 999,
                "sha1": "ABCDEF",
                "is_dir": False,
            },
            "missing-fid": {
                "version": 1,
                "kind": "cloud115_media",
                "fid": "missing-fid",
                "parent_cid": "parent-cid",
                "pickcode": "missing-pickcode",
                "name": "missing.mp4",
                "size_bytes": 10,
                "sha1": "MISSING",
                "is_dir": False,
            },
        }

    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade._scan_cloud115_media_refs",
        fake_scan,
    )

    summary = upgrade_v053_database(clean_db)

    assert summary.media_count == 3
    assert summary.invalid_media_count == 0
    assert classify_database_schema(clean_db) == "current"
    assert LEGACY_V053_UPGRADE_MIGRATION_NAME in {
        row.name for row in SchemaMigration.select()
    }

    library_columns = _column_names(clean_db, "media_library")
    assert {"provider_key", "provider_config", "account_key"} <= library_columns
    assert not {"backend", "backend_config", "backend_account_key"} & library_columns
    media_columns = _column_names(clean_db, "media")
    assert {"storage_ref", "file_name", "file_hash"} <= media_columns
    assert (
        not {
            "path",
            "backend_locator",
            "storage_mode",
            "content_fingerprint",
            "thumbnail_source_fingerprint",
        }
        & media_columns
    )

    local_library = MediaLibrary.get_by_id(ids["local_library_id"])
    assert local_library.provider_key == "local"
    assert local_library.provider_config["media_root_path"] == str(tmp_path / "media")
    local_media = Media.get_by_id(ids["local_media_id"])
    assert local_media.storage_ref == {
        "version": 1,
        "kind": "media_local_path",
        "relative_path": "jav/ABP-001.mp4",
    }

    cloud_library = MediaLibrary.get_by_id(ids["cloud_library_id"])
    assert cloud_library.provider_key == "cloud115"
    assert cloud_library.account_key == "12345"
    assert cloud_library.provider_config["device_cookie"].startswith("UID=12345_")
    cloud_media = Media.get_by_id(ids["cloud_media_id"])
    assert cloud_media.id == ids["cloud_media_id"]
    assert cloud_media.file_name == "ABP-001-renamed.mp4"
    assert cloud_media.file_size_bytes == 999
    assert cloud_media.file_hash is None
    assert cloud_media.storage_ref["parent_cid"] == "parent-cid"
    missing_media = Media.get_by_id(ids["missing_media_id"])
    assert missing_media.valid is True
    assert missing_media.storage_ref["fid"] == "missing-fid"

    assert MediaProgress.get(MediaProgress.media == cloud_media).position_seconds == 45
    assert (
        MediaPoint.get(MediaPoint.media == cloud_media).thumbnail_id
        == ids["thumbnail_id"]
    )
    assert MediaClip.get(MediaClip.media == cloud_media).file_path == "ABP-001/clip.mp4"
    thumbnail = MediaThumbnail.get_by_id(ids["thumbnail_id"])
    assert thumbnail.image_search_index_status == 0
    assert MoviePlotImage.get_by_id(ids["plot_link_id"]).image_search_index_status == 0
    assert Subtitle.get_by_id(ids["subtitle_id"]).file_path == "ABP-001/ABP-001.srt"
    assert Indexer.get_by_id(ids["indexer_id"]).name == "保留索引器"
    assert "download_client" not in clean_db.get_tables()
    assert "download_task" not in clean_db.get_tables()

    ordinary_summary = run_pending_migrations(clean_db)
    assert ordinary_summary.applied_count == 1
    clean_db.create_tables(TEST_MODELS)
    assert run_pending_migrations(clean_db).applied_count == 0
    assert "special_tags" not in _column_names(clean_db, "media")
    assert "image_search_session" in clean_db.get_tables()
    assert DownloadClient.select().count() == 0
    assert IndexerDownloadClient.select().count() == 0
    assert DownloadTask.select().count() == 0


def test_ordinary_migrate_refuses_unconverted_v053_before_destructive_changes(clean_db):
    _create_v053_schema(clean_db)

    with pytest.raises(ValueError, match="legacy_v053_upgrade_required"):
        run_pending_migrations(clean_db)

    assert "special_tags" in _column_names(clean_db, "media")


def test_v053_upgrade_rejects_orphan_media_before_writing(clean_db):
    _create_v053_schema(clean_db)
    clean_db.execute_sql(
        """
        INSERT INTO media (
            movie_number, library_id, path, file_size_bytes, duration_seconds,
            valid, thumbnail_generation_state, thumbnail_attempt_count,
            thumbnail_deferred_count, created_at, updated_at
        ) VALUES (NULL, NULL, '/missing.mp4', 1, 1, TRUE, 'pending',
                  0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )

    with pytest.raises(LegacyV053UpgradeError, match="orphan_media"):
        upgrade_v053_database(clean_db)

    assert classify_database_schema(clean_db) == "legacy_v053"


def test_v053_upgrade_rejects_newly_invalid_media_before_writing(
    clean_db, tmp_path, monkeypatch
):
    _create_v053_schema(clean_db)
    _insert_v053_data(clean_db, tmp_path)

    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade._scan_cloud115_media_refs",
        lambda _provider_config, _library_id: {},
    )

    with pytest.raises(
        LegacyV053UpgradeError,
        match="upgrade_would_invalidate_media: count=2",
    ):
        upgrade_v053_database(clean_db)

    assert classify_database_schema(clean_db) == "legacy_v053"
    assert "storage_ref" not in _column_names(clean_db, "media")
