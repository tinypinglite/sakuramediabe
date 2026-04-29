import toml
import math
import pytest

import src.config.config as config_module
from src.config.config import (
    Auth,
    Database,
    DatabaseEngine,
    ImageSearch,
    IndexerSettings,
    IndexerType,
    LanceDb,
    Logging,
    Metadata,
    MovieInfoTranslation,
    Scheduler,
    Settings,
)


def test_database_defaults_to_sqlite_file():
    database = Database()

    assert database.engine is DatabaseEngine.SQLITE
    assert database.path == "/data/db/sakuramedia.db"


def test_image_search_defaults_to_remote_inference_endpoint():
    image_search = ImageSearch()

    assert image_search.inference_base_url == "http://joytag-infer:8001"


def test_settings_can_be_built_without_config_file(tmp_path, monkeypatch):
    missing_config_path = tmp_path / "missing-config.toml"
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", missing_config_path)

    settings = Settings()

    assert settings.database.engine is DatabaseEngine.SQLITE
    assert settings.database.path == "/data/db/sakuramedia.db"
    assert settings.auth.username == "account"
    assert settings.scheduler.enabled is True
    assert settings.scheduler.actor_subscription_sync_cron == "0 2 * * *"
    assert settings.scheduler.subscribed_movie_auto_download_cron == "30 2 * * *"
    assert settings.scheduler.download_task_sync_cron == "* * * * *"
    assert settings.scheduler.download_task_auto_import_cron == "*/3 * * * *"
    assert settings.scheduler.movie_collection_sync_cron == "0 1 * * *"
    assert settings.scheduler.movie_heat_cron == "15 0 * * *"
    assert settings.scheduler.movie_interaction_sync_cron == "0 * * * *"
    assert settings.scheduler.ranking_sync_cron == "45 1 * * *"
    assert settings.scheduler.hot_review_sync_cron == "20 1 * * *"
    assert settings.logging.level == "INFO"
    assert isinstance(settings.auth.file_signature_secret, str)
    assert settings.auth.file_signature_secret
    assert settings.metadata.gfriends_filetree_url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
    assert settings.metadata.gfriends_cdn_base_url == "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends"
    assert settings.metadata.gfriends_filetree_cache_path == "/data/cache/gfriends/gfriends-filetree.json"
    assert settings.metadata.gfriends_filetree_cache_ttl_hours == 168
    assert settings.metadata.normalized_dmm_proxy is None
    assert settings.media.collection_duration_threshold_minutes == 300
    assert settings.media.max_thumbnail_process_count == max(
        1, math.ceil(((config_module.os.cpu_count() or 1) / 2))
    )
    assert settings.scheduler.media_thumbnail_cron == "*/5 * * * *"
    assert settings.scheduler.image_search_index_cron == "0 0 * * *"
    assert settings.scheduler.image_search_optimize_cron == "0 3 * * *"
    assert settings.scheduler.movie_desc_sync_cron == "0 4 * * *"
    assert settings.scheduler.movie_desc_translation_cron == "15 4 * * *"
    assert settings.scheduler.movie_title_translation_cron == "20 4 * * *"
    assert settings.movie_info_translation == MovieInfoTranslation()
    assert settings.image_search == ImageSearch()
    assert settings.lancedb == LanceDb()


def test_settings_loads_metadata_gfriends_settings_from_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(
            {
                "metadata": {
                    "javdb_host": "example.com",
                    "proxy": "http://127.0.0.1:7890",
                    "dmm_proxy": "  http://127.0.0.1:7890  ",
                    "gfriends_filetree_url": "https://cdn.example.com/Filetree.json",
                    "gfriends_cdn_base_url": "https://cdn.example.com",
                    "gfriends_filetree_cache_path": "./tmp/gfriends.json",
                    "gfriends_filetree_cache_ttl_hours": 24,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    settings = config_module.Settings()

    assert settings.metadata.javdb_host == "example.com"
    assert settings.metadata.proxy == "http://127.0.0.1:7890"
    assert settings.metadata.normalized_proxy == "http://127.0.0.1:7890"
    assert settings.metadata.gfriends_proxy == "http://127.0.0.1:7890"
    assert settings.metadata.normalized_dmm_proxy == "http://127.0.0.1:7890"
    assert settings.metadata.gfriends_filetree_url == "https://cdn.example.com/Filetree.json"
    assert settings.metadata.gfriends_cdn_base_url == "https://cdn.example.com"
    assert settings.metadata.gfriends_filetree_cache_path == "./tmp/gfriends.json"
    assert settings.metadata.gfriends_filetree_cache_ttl_hours == 24


@pytest.mark.parametrize(
    ("raw_proxy", "expected_proxy"),
    [
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("  http://127.0.0.1:7890  ", "http://127.0.0.1:7890"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_metadata_proxy_normalization(raw_proxy, expected_proxy):
    metadata = Metadata(proxy=raw_proxy)

    assert metadata.normalized_proxy == expected_proxy
    assert metadata.gfriends_proxy == expected_proxy
    assert metadata.normalized_dmm_proxy == expected_proxy


@pytest.mark.parametrize(
    ("raw_proxy", "expected_proxy"),
    [
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("  http://127.0.0.1:7890  ", "http://127.0.0.1:7890"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_metadata_legacy_dmm_proxy_fallback(raw_proxy, expected_proxy):
    metadata = Metadata(dmm_proxy=raw_proxy)

    assert metadata.normalized_proxy == expected_proxy
    assert metadata.gfriends_proxy == expected_proxy
    assert metadata.normalized_dmm_proxy == expected_proxy


def test_metadata_proxy_takes_priority_over_legacy_dmm_proxy():
    metadata = Metadata(
        proxy="  http://127.0.0.1:7890  ",
        dmm_proxy="http://127.0.0.1:7891",
    )

    assert metadata.normalized_proxy == "http://127.0.0.1:7890"
    assert metadata.gfriends_proxy == "http://127.0.0.1:7890"
    assert metadata.normalized_dmm_proxy == "http://127.0.0.1:7890"


@pytest.mark.parametrize(
    ("raw_proxy", "expected_proxy"),
    [
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("  http://127.0.0.1:7890  ", "http://127.0.0.1:7890"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_metadata_license_proxy_normalization(raw_proxy, expected_proxy):
    metadata = Metadata(license_proxy=raw_proxy)

    assert metadata.normalized_license_proxy == expected_proxy


def test_auth_generates_random_file_signature_secret_by_default():
    first_auth = Auth()
    second_auth = Auth()

    assert first_auth.file_signature_secret
    assert second_auth.file_signature_secret
    assert first_auth.file_signature_secret != second_auth.file_signature_secret


def test_settings_ignore_legacy_file_signature_expire_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(
            {
                "auth": {
                    "username": "legacy-account",
                    "file_signature_expire_seconds": 900,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    settings = config_module.Settings()

    # 兼容旧配置残留，但不再把签名过期时间作为可配置字段暴露。
    assert settings.auth.username == "legacy-account"
    assert not hasattr(settings.auth, "file_signature_expire_seconds")


def test_settings_loads_indexer_settings_from_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(
            {
                "indexer_settings": {
                    "type": "jackett",
                    "api_key": "secret-key",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    settings = config_module.Settings()

    assert settings.indexer_settings.type is IndexerType.JACKETT
    assert settings.indexer_settings.api_key == "secret-key"


def test_settings_loads_scheduler_settings_from_config_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(
            {
                "scheduler": {
                    "enabled": False,
                    "log_dir": "./tmp/logs",
                    "actor_subscription_sync_cron": "15 3 * * *",
                    "subscribed_movie_auto_download_cron": "45 3 * * *",
                    "download_task_sync_cron": "*/20 * * * *",
                    "download_task_auto_import_cron": "*/12 * * * *",
                    "movie_collection_sync_cron": "0 4 * * *",
                    "movie_heat_cron": "30 1 * * *",
                    "movie_interaction_sync_cron": "40 5 * * *",
                    "ranking_sync_cron": "10 4 * * *",
                    "hot_review_sync_cron": "20 4 * * *",
                    "movie_desc_sync_cron": "35 4 * * *",
                    "movie_desc_translation_cron": "50 4 * * *",
                    "movie_title_translation_cron": "55 4 * * *",
                    "media_thumbnail_cron": "*/7 * * * *",
                    "image_search_index_cron": "*/8 * * * *",
                    "image_search_optimize_cron": "30 */4 * * *",
                },
                "movie_info_translation": {
                    "enabled": True,
                    "base_url": "http://llm.internal:9000",
                    "api_key": "llm-token",
                    "model": "custom-translator",
                    "timeout_seconds": 120,
                    "connect_timeout_seconds": 7,
                },
                "media": {
                    "collection_duration_threshold_minutes": 360,
                    "max_thumbnail_process_count": 9,
                },
                "image_search": {
                    "inference_base_url": "http://joytag-infer.internal:8010",
                    "inference_timeout_seconds": 45,
                    "inference_connect_timeout_seconds": 5,
                    "inference_api_key": "secret-token",
                    "inference_batch_size": 8,
                    "session_ttl_seconds": 1200,
                    "default_page_size": 15,
                    "max_page_size": 60,
                    "search_scan_batch_size": 30,
                },
                "lancedb": {
                    "uri": "./tmp/lancedb",
                    "table_name": "custom_thumbnail_vectors",
                    "vector_dtype": "float16",
                    "distance_metric": "cosine",
                    "vector_index_type": "ivf_rq",
                    "vector_index_num_partitions": 256,
                    "vector_index_num_bits": 1,
                    "vector_index_num_sub_vectors": 32,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    settings = config_module.Settings()

    assert settings.scheduler.enabled is False
    assert settings.scheduler.log_dir == "./tmp/logs"
    assert settings.scheduler.actor_subscription_sync_cron == "15 3 * * *"
    assert settings.scheduler.subscribed_movie_auto_download_cron == "45 3 * * *"
    assert settings.scheduler.download_task_sync_cron == "*/20 * * * *"
    assert settings.scheduler.download_task_auto_import_cron == "*/12 * * * *"
    assert settings.scheduler.movie_collection_sync_cron == "0 4 * * *"
    assert settings.scheduler.movie_heat_cron == "30 1 * * *"
    assert settings.scheduler.movie_interaction_sync_cron == "40 5 * * *"
    assert settings.scheduler.ranking_sync_cron == "10 4 * * *"
    assert settings.scheduler.hot_review_sync_cron == "20 4 * * *"
    assert settings.scheduler.movie_desc_sync_cron == "35 4 * * *"
    assert settings.scheduler.movie_desc_translation_cron == "50 4 * * *"
    assert settings.scheduler.movie_title_translation_cron == "55 4 * * *"
    assert settings.scheduler.media_thumbnail_cron == "*/7 * * * *"
    assert settings.scheduler.image_search_index_cron == "*/8 * * * *"
    assert settings.scheduler.image_search_optimize_cron == "30 */4 * * *"
    assert settings.media.collection_duration_threshold_minutes == 360
    assert settings.media.max_thumbnail_process_count == 9
    assert settings.movie_info_translation.enabled is True
    assert settings.movie_info_translation.base_url == "http://llm.internal:9000"
    assert settings.movie_info_translation.api_key == "llm-token"
    assert settings.movie_info_translation.model == "custom-translator"
    assert settings.movie_info_translation.timeout_seconds == 120
    assert settings.movie_info_translation.connect_timeout_seconds == 7
    assert settings.image_search.inference_base_url == "http://joytag-infer.internal:8010"
    assert settings.image_search.inference_timeout_seconds == 45
    assert settings.image_search.inference_connect_timeout_seconds == 5
    assert settings.image_search.inference_api_key == "secret-token"
    assert settings.image_search.inference_batch_size == 8
    assert settings.image_search.session_ttl_seconds == 1200
    assert settings.image_search.default_page_size == 15
    assert settings.image_search.max_page_size == 60
    assert settings.image_search.search_scan_batch_size == 30
    assert settings.lancedb.uri == "./tmp/lancedb"
    assert settings.lancedb.table_name == "custom_thumbnail_vectors"
    assert settings.lancedb.vector_dtype == "float16"
    assert settings.lancedb.distance_metric == "cosine"
    assert settings.lancedb.vector_index_type == "ivf_rq"
    assert settings.lancedb.vector_index_num_partitions == 256
    assert settings.lancedb.vector_index_num_bits == 1
    assert settings.lancedb.vector_index_num_sub_vectors == 32


def test_update_settings_writes_indexer_settings_and_refreshes_runtime_state(
    tmp_path,
    monkeypatch,
):
    original_runtime_settings = config_module.Settings.model_validate(
        config_module.settings.model_dump()
    )
    config_path = tmp_path / "config.toml"
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    new_settings = config_module.Settings.model_validate(
        original_runtime_settings.model_dump()
    )
    new_settings.indexer_settings = IndexerSettings(
        type=IndexerType.JACKETT,
        api_key="updated-secret-key",
    )

    try:
        current_file_signature_secret = new_settings.auth.file_signature_secret
        new_settings.scheduler = Scheduler(
            enabled=True,
            log_dir="./logs/tasks",
            actor_subscription_sync_cron="0 2 * * *",
            subscribed_movie_auto_download_cron="30 2 * * *",
            download_task_sync_cron="*/15 * * * *",
            download_task_auto_import_cron="*/10 * * * *",
            movie_collection_sync_cron="0 1 * * *",
            movie_heat_cron="15 0 * * *",
            movie_interaction_sync_cron="0 * * * *",
            ranking_sync_cron="45 1 * * *",
            hot_review_sync_cron="20 1 * * *",
            media_file_scan_cron="0 */6 * * *",
            movie_desc_sync_cron="0 4 * * *",
            movie_desc_translation_cron="15 4 * * *",
            movie_title_translation_cron="20 4 * * *",
            media_thumbnail_cron="*/5 * * * *",
            image_search_index_cron="*/10 * * * *",
            image_search_optimize_cron="0 */6 * * *",
        )
        new_settings.media.collection_duration_threshold_minutes = 420
        new_settings.media.max_thumbnail_process_count = 6
        new_settings.movie_info_translation = MovieInfoTranslation(
            enabled=True,
            base_url="http://llm.internal:9000",
            api_key="updated-llm-token",
            model="translator-v2",
            timeout_seconds=150,
            connect_timeout_seconds=6,
        )
        new_settings.logging = Logging(level="DEBUG")
        new_settings.image_search = ImageSearch(
            inference_base_url="http://joytag-infer.internal:8001",
            inference_timeout_seconds=40,
            inference_connect_timeout_seconds=6,
            inference_api_key="updated-infer-token",
            inference_batch_size=12,
            session_ttl_seconds=1800,
            default_page_size=25,
            max_page_size=90,
            search_scan_batch_size=40,
        )
        new_settings.lancedb = LanceDb(
            uri="/var/lib/lancedb",
            table_name="media_thumbnail_vectors_v2",
            vector_dtype="float16",
            distance_metric="cosine",
            vector_index_type="ivf_rq",
            vector_index_num_partitions=512,
            vector_index_num_bits=1,
            vector_index_num_sub_vectors=96,
        )
        new_settings.metadata = Metadata(
            javdb_host="updated-host.example",
            proxy="http://127.0.0.1:7890",
            license_proxy="http://127.0.0.1:7891",
            gfriends_filetree_url="https://cdn.example.com/Filetree.json",
            gfriends_cdn_base_url="https://cdn.example.com",
            gfriends_filetree_cache_path="./tmp/gfriends.json",
            gfriends_filetree_cache_ttl_hours=48,
        )
        assert config_module.update_settings(new_settings) is True

        persisted = toml.loads(config_path.read_text(encoding="utf-8"))
        assert persisted["indexer_settings"]["type"] == "jackett"
        assert persisted["indexer_settings"]["api_key"] == "updated-secret-key"
        assert "file_signature_secret" not in persisted["auth"]
        assert persisted["scheduler"] == {
            "enabled": True,
            "log_dir": "./logs/tasks",
            "actor_subscription_sync_cron": "0 2 * * *",
            "subscribed_movie_auto_download_cron": "30 2 * * *",
            "download_task_sync_cron": "*/15 * * * *",
            "download_task_auto_import_cron": "*/10 * * * *",
            "movie_collection_sync_cron": "0 1 * * *",
            "movie_heat_cron": "15 0 * * *",
            "movie_interaction_sync_cron": "0 * * * *",
            "ranking_sync_cron": "45 1 * * *",
            "hot_review_sync_cron": "20 1 * * *",
            "media_file_scan_cron": "0 */6 * * *",
            "movie_desc_sync_cron": "0 4 * * *",
            "movie_desc_translation_cron": "15 4 * * *",
            "movie_title_translation_cron": "20 4 * * *",
            "media_thumbnail_cron": "*/5 * * * *",
            "image_search_index_cron": "*/10 * * * *",
            "image_search_optimize_cron": "0 */6 * * *",
            "movie_similarity_recompute_cron": "30 3 * * *",
        }
        assert persisted["media"]["collection_duration_threshold_minutes"] == 420
        assert persisted["media"]["max_thumbnail_process_count"] == 6
        assert persisted["movie_info_translation"] == {
            "enabled": True,
            "base_url": "http://llm.internal:9000",
            "api_key": "updated-llm-token",
            "model": "translator-v2",
            "timeout_seconds": 150.0,
            "connect_timeout_seconds": 6.0,
        }
        assert persisted["image_search"] == {
            "inference_base_url": "http://joytag-infer.internal:8001",
            "inference_timeout_seconds": 40.0,
            "inference_connect_timeout_seconds": 6.0,
            "inference_api_key": "updated-infer-token",
            "inference_batch_size": 12,
            "session_ttl_seconds": 1800,
            "default_page_size": 25,
            "max_page_size": 90,
            "search_scan_batch_size": 40,
            "index_upsert_batch_size": 100,
            "optimize_every_records": 5000,
            "optimize_every_seconds": 1800,
            "optimize_on_job_end": True,
        }
        assert persisted["lancedb"] == {
            "uri": "/var/lib/lancedb",
            "table_name": "media_thumbnail_vectors_v2",
            "vector_dtype": "float16",
            "distance_metric": "cosine",
            "vector_index_type": "ivf_rq",
            "vector_index_num_partitions": 512,
            "vector_index_num_bits": 1,
            "vector_index_num_sub_vectors": 96,
            "scalar_index_columns": ["movie_id"],
        }
        assert persisted["logging"] == {
            "level": "DEBUG",
        }
        assert persisted["metadata"] == {
            "javdb_host": "updated-host.example",
            "proxy": "http://127.0.0.1:7890",
            "license_proxy": "http://127.0.0.1:7891",
            "gfriends_filetree_url": "https://cdn.example.com/Filetree.json",
            "gfriends_cdn_base_url": "https://cdn.example.com",
            "gfriends_filetree_cache_path": "./tmp/gfriends.json",
            "gfriends_filetree_cache_ttl_hours": 48,
            "import_metadata_max_workers": 3,
        }
        assert config_module.settings.indexer_settings.api_key == "updated-secret-key"
        assert config_module.settings.scheduler.actor_subscription_sync_cron == "0 2 * * *"
        assert config_module.settings.scheduler.subscribed_movie_auto_download_cron == "30 2 * * *"
        assert config_module.settings.scheduler.download_task_sync_cron == "*/15 * * * *"
        assert config_module.settings.scheduler.download_task_auto_import_cron == "*/10 * * * *"
        assert config_module.settings.scheduler.movie_collection_sync_cron == "0 1 * * *"
        assert config_module.settings.scheduler.movie_heat_cron == "15 0 * * *"
        assert config_module.settings.scheduler.movie_interaction_sync_cron == "0 * * * *"
        assert config_module.settings.scheduler.ranking_sync_cron == "45 1 * * *"
        assert config_module.settings.scheduler.hot_review_sync_cron == "20 1 * * *"
        assert config_module.settings.scheduler.media_file_scan_cron == "0 */6 * * *"
        assert config_module.settings.scheduler.movie_desc_translation_cron == "15 4 * * *"
        assert config_module.settings.scheduler.movie_title_translation_cron == "20 4 * * *"
        assert config_module.settings.scheduler.image_search_index_cron == "*/10 * * * *"
        assert config_module.settings.scheduler.movie_similarity_recompute_cron == "30 3 * * *"
        assert config_module.settings.media.collection_duration_threshold_minutes == 420
        assert config_module.settings.movie_info_translation.model == "translator-v2"
        assert config_module.settings.image_search.inference_base_url == "http://joytag-infer.internal:8001"
        assert config_module.settings.image_search.search_scan_batch_size == 40
        assert config_module.settings.lancedb.uri == "/var/lib/lancedb"
        assert config_module.settings.lancedb.table_name == "media_thumbnail_vectors_v2"
        assert config_module.settings.metadata.gfriends_filetree_cache_ttl_hours == 48
        assert config_module.settings.auth.file_signature_secret == current_file_signature_secret
    finally:
        config_module.refresh_runtime_settings(original_runtime_settings)


def test_settings_supports_legacy_movie_desc_translation_section(tmp_path, monkeypatch):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(
            {
                "movie_desc_translation": {
                    "enabled": True,
                    "base_url": "http://legacy-llm:9000",
                    "api_key": "legacy-token",
                    "model": "legacy-model",
                    "timeout_seconds": 180,
                    "connect_timeout_seconds": 8,
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    settings = config_module.Settings()

    assert settings.movie_info_translation == MovieInfoTranslation(
        enabled=True,
        base_url="http://legacy-llm:9000",
        api_key="legacy-token",
        model="legacy-model",
        timeout_seconds=180,
        connect_timeout_seconds=8,
    )
