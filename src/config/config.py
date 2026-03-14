#!/usr/bin/env python
# -*- encoding: utf-8 -*-

import json
import math
import os
import pathlib
import secrets
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Tuple, Type
from loguru import logger
import toml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class DatabaseEngine(str, Enum):
    SQLITE = "sqlite"
    MYSQL = "mysql"
    POSTGRES = "postgres"


class IndexerType(str, Enum):
    JACKETT = "jackett"


class IndexerKind(str, Enum):
    PT = "pt"
    BT = "bt"


class Database(BaseModel):
    engine: DatabaseEngine = DatabaseEngine.SQLITE
    path: str = "/data/db/sakuramedia.db"
    charset: str = "utf8mb4"
    url: str = ""
    pragmas: dict[str, Any] = Field(default_factory=lambda: {"foreign_keys": 1})


class Auth(BaseModel):
    username: str = "account"
    password: str = "account"
    secret_key: str = "98765432178965437"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 30
    refresh_token_expire_minutes: int = 60 * 24 * 7
    file_signature_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    file_signature_expire_seconds: int = 900


class Media(BaseModel):
    others_number_features: set[str] = Field(default_factory=lambda: {
        "OFJE", "CJOB", "DVAJ", "REBD"
    })
    inner_sub_tags: set[str] = Field(
        default_factory=lambda: {"中字", "中文", "字幕组", "-UC", "-C"}
    )
    blueray_tags: set[str] = Field(default_factory=lambda: {"蓝光", "4K", "4k"})
    uncensored_tags: set[str] = Field(
        default_factory=lambda: {
            "流出",
            "uncensored",
            "無码",
            "無修正",
            "UC",
            "无码",
            "破解",
            "UNCENSORED",
            "-UC",
            "-U",
        }
    )
    uncensored_prefix: set[str] = Field(
        default_factory=lambda: {
            "PT-",
            "S2M",
            "BT",
            "LAF",
            "SMD",
            "SMBD",
            "SM3D2DBD",
            "SKY-",
            "SKYHD",
            "CWP",
            "CWDV",
            "CWBD",
            "CW3D2DBD",
            "MKD",
            "MKBD",
            "MXBD",
            "MK3D2DBD",
            "MCB3DBD",
            "MCBD",
            "RHJ",
            "MMDV",
        }
    )
    allowed_min_video_file_size: int = 1024 * 1024 * 1024
    import_image_root_path: str = "/data/cache/assets"
    max_thumbnail_process_count: int = Field(
        default_factory=lambda: max(1, math.ceil((os.cpu_count() or 1) / 2))
    )


class Metadata(BaseModel):
    javdb_host: str = "jdforrepam.com"
    proxy: str | None = None
    gfriends_filetree_url: str = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
    gfriends_cdn_base_url: str = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends"
    gfriends_filetree_cache_path: str = "/data/cache/gfriends/gfriends-filetree.json"
    gfriends_filetree_cache_ttl_hours: int = 24 * 7
    import_metadata_max_workers: int = 3


class Scheduler(BaseModel):
    enabled: bool = True
    timezone: str = "Asia/Shanghai"
    log_dir: str = "/data/logs"
    actor_subscription_sync_cron: str = "0 2 * * *"
    subscribed_movie_auto_download_cron: str = "30 2 * * *"
    download_task_sync_cron: str = "* * * * *"
    download_task_auto_import_cron: str = "*/3 * * * *"
    movie_collection_sync_cron: str = "0 1 * * *"
    movie_heat_cron: str = "15 0 * * *"
    media_thumbnail_cron: str = "*/5 * * * *"
    image_search_index_cron: str = "*/10 * * * *"
    image_search_optimize_cron: str = "0 */6 * * *"


class Logging(BaseModel):
    level: str = "INFO"


class IndexerSettings(BaseModel):
    type: IndexerType = IndexerType.JACKETT
    api_key: str = "change-me"


class ImageSearch(BaseModel):
    joytag_model_dir: str = "/data/lib/joytag"
    prefer_gpu: bool = True
    cpu_threads: int | None = None
    session_ttl_seconds: int = 600
    default_page_size: int = 20
    max_page_size: int = 100
    search_scan_batch_size: int = 100


class LanceDb(BaseModel):
    uri: str = "/data/indexes/image-search"
    table_name: str = "media_thumbnail_vectors"
    vector_dtype: Literal["float16"] = "float16"
    distance_metric: Literal["cosine"] = "cosine"
    vector_index_type: Literal["ivf_rq", "ivf_pq"] = "ivf_rq"
    vector_index_num_partitions: int = 512
    vector_index_num_bits: int = 1
    vector_index_num_sub_vectors: int = 96


if Path('/data/config/config.toml').exists():
    SETTINGS_TOML_PATH = Path('/data/config/config.toml')
else:
    logger.warning("No config.toml found at /data/config/config.toml, using default config.toml path.")
    SETTINGS_TOML_PATH = pathlib.Path(__file__).parent / "config.toml"


class Settings(BaseSettings):
    database: Database = Field(default_factory=Database)
    auth: Auth = Field(default_factory=Auth)
    media: Media = Field(default_factory=Media)
    metadata: Metadata = Field(default_factory=Metadata)
    scheduler: Scheduler = Field(default_factory=Scheduler)
    logging: Logging = Field(default_factory=Logging)
    indexer_settings: IndexerSettings = Field(default_factory=IndexerSettings)
    image_search: ImageSearch = Field(default_factory=ImageSearch)
    lancedb: LanceDb = Field(default_factory=LanceDb)
    enable_docs: bool = False

    model_config = SettingsConfigDict(
        toml_file=SETTINGS_TOML_PATH
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            TomlConfigSettingsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
        )


def get_settings() -> Settings:
    return Settings()


settings = Settings()


def refresh_runtime_settings(new_settings: Settings) -> None:
    for field_name in Settings.model_fields:
        setattr(settings, field_name, getattr(new_settings, field_name))


def _build_persistable_settings(settings_to_persist: Settings) -> dict[str, Any]:
    serializable_settings = json.loads(settings_to_persist.model_dump_json())
    auth_settings = serializable_settings.get("auth")
    if isinstance(auth_settings, dict):
        auth_settings.pop("file_signature_secret", None)
    return serializable_settings


def update_settings(new_settings: Settings) -> bool:
    serializable_settings = _build_persistable_settings(new_settings)
    settings_path = Path(Settings.model_config["toml_file"])
    with open(settings_path, "w", encoding="utf-8") as file:
        file.write(toml.dumps(serializable_settings))
    refresh_runtime_settings(new_settings)
    return True
