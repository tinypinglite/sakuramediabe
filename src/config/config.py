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
from pydantic import AliasChoices, BaseModel, Field, model_validator
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


class Media(BaseModel):
    others_number_features: set[str] = Field(default_factory=lambda: {
        "OFJE", "CJOB", "DVAJ", "REBD"
    })
    collection_duration_threshold_minutes: int = 300
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
    subtitle_root_path: str = "/data/cache/subtitles"
    max_thumbnail_process_count: int = Field(
        default_factory=lambda: max(1, math.ceil((os.cpu_count() or 1) / 2))
    )


class MovieInfoTranslation(BaseModel):
    enabled: bool = False
    base_url: str = "http://localhost:8000"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout_seconds: float = 300.0
    connect_timeout_seconds: float = 3.0


# 兼容现有导入路径，运行时统一使用 MovieInfoTranslation。
MovieDescTranslation = MovieInfoTranslation


class Metadata(BaseModel):
    javdb_host: str = "apidd.btyjscl.com"
    proxy: str | None = None
    license_proxy: str | None = None
    # 兼容旧配置项：新版本统一使用 proxy，dmm_proxy 仅在 proxy 为空时作为读取回退。
    dmm_proxy: str | None = Field(default=None, exclude=True)
    gfriends_filetree_url: str = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
    gfriends_cdn_base_url: str = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends"
    gfriends_filetree_cache_path: str = "/data/cache/gfriends/gfriends-filetree.json"
    gfriends_filetree_cache_ttl_hours: int = 24 * 7
    import_metadata_max_workers: int = 3

    @property
    def normalized_proxy(self) -> str | None:
        # 统一在配置层做代理值归一化；旧 dmm_proxy 只作为老用户配置回退。
        proxy = (self.proxy or "").strip()
        if proxy:
            return proxy
        return (self.dmm_proxy or "").strip() or None

    @property
    def normalized_license_proxy(self) -> str | None:
        # 授权中心代理与站点代理隔离，避免 metadata.proxy 误影响授权请求。
        return (self.license_proxy or "").strip() or None

    @property
    def gfriends_proxy(self) -> str | None:
        # 兼容仅配置了旧 dmm_proxy 的用户，统一代理仍可作用于 GFriends。
        return self.normalized_proxy

    @property
    def normalized_dmm_proxy(self) -> str | None:
        # 兼容旧代码读路径，实际代理策略统一由 normalized_proxy 决定。
        return self.normalized_proxy


class Scheduler(BaseModel):
    enabled: bool = True
    log_dir: str = "/data/logs"
    actor_subscription_sync_cron: str = "0 2 * * *"
    subscribed_movie_auto_download_cron: str = "30 2 * * *"
    download_task_sync_cron: str = "* * * * *"
    download_task_auto_import_cron: str = "*/3 * * * *"
    movie_collection_sync_cron: str = "0 1 * * *"
    movie_heat_cron: str = "15 0 * * *"
    movie_interaction_sync_cron: str = "0 * * * *"
    ranking_sync_cron: str = "45 1 * * *"
    hot_review_sync_cron: str = "20 1 * * *"
    media_file_scan_cron: str = "0 */6 * * *"
    movie_desc_sync_cron: str = "0 4 * * *"
    movie_desc_translation_cron: str = "15 4 * * *"
    movie_title_translation_cron: str = "20 4 * * *"
    media_thumbnail_cron: str = "*/5 * * * *"
    image_search_index_cron: str = "0 0 * * *"
    image_search_optimize_cron: str = "0 3 * * *"
    movie_similarity_recompute_cron: str = "30 3 * * *"
    metadata_provider_license_renew_cron: str = "0 */6 * * *"


class Logging(BaseModel):
    level: str = "INFO"


class IndexerSettings(BaseModel):
    type: IndexerType = IndexerType.JACKETT
    api_key: str = "change-me"


class ImageSearch(BaseModel):
    inference_base_url: str = "http://joytag-infer:8001"
    inference_timeout_seconds: float = 30.0
    inference_connect_timeout_seconds: float = 3.0
    inference_api_key: str | None = None
    inference_batch_size: int = 16
    session_ttl_seconds: int = 600
    default_page_size: int = 20
    max_page_size: int = 100
    search_scan_batch_size: int = 100
    index_upsert_batch_size: int = 100
    optimize_every_records: int = 5000
    optimize_every_seconds: int = 1800
    optimize_on_job_end: bool = True


class LanceDb(BaseModel):
    uri: str = "/data/indexes/image-search"
    table_name: str = "media_thumbnail_vectors"
    vector_dtype: Literal["float16"] = "float16"
    distance_metric: Literal["cosine"] = "cosine"
    vector_index_type: Literal["ivf_rq", "ivf_pq"] = "ivf_rq"
    vector_index_num_partitions: int = 512
    vector_index_num_bits: int = 1
    vector_index_num_sub_vectors: int = 96
    scalar_index_columns: list[str] = Field(default_factory=lambda: ["movie_id"])


if Path('/data/config/config.toml').exists():
    SETTINGS_TOML_PATH = Path('/data/config/config.toml')
else:
    logger.warning("No config.toml found at /data/config/config.toml, using default config.toml path.")
    SETTINGS_TOML_PATH = pathlib.Path(__file__).parent / "config.toml"


class Settings(BaseSettings):
    database: Database = Field(default_factory=Database)
    auth: Auth = Field(default_factory=Auth)
    media: Media = Field(default_factory=Media)
    movie_info_translation: MovieInfoTranslation = Field(
        default_factory=MovieInfoTranslation,
        validation_alias=AliasChoices("movie_info_translation", "movie_desc_translation"),
    )
    metadata: Metadata = Field(default_factory=Metadata)
    scheduler: Scheduler = Field(default_factory=Scheduler)
    logging: Logging = Field(default_factory=Logging)
    indexer_settings: IndexerSettings = Field(default_factory=IndexerSettings)
    image_search: ImageSearch = Field(default_factory=ImageSearch)
    lancedb: LanceDb = Field(default_factory=LanceDb)
    enable_docs: bool = False

    model_config = SettingsConfigDict(
        toml_file=SETTINGS_TOML_PATH,
        extra="ignore",
    )

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_movie_translation_settings(cls, data: Any):
        if not isinstance(data, dict):
            return data
        normalized_data = dict(data)
        # 兼容历史遗留的媒体音频识别配置节，读取时直接忽略，避免旧 config.toml 导致启动失败。
        normalized_data.pop("_".join(("media", "asr")), None)
        if "movie_info_translation" not in normalized_data and "movie_desc_translation" in normalized_data:
            # 兼容旧配置节名称，统一映射到新的共享翻译配置上。
            normalized_data["movie_info_translation"] = normalized_data["movie_desc_translation"]
        return normalized_data

    @property
    def movie_desc_translation(self) -> MovieInfoTranslation:
        # 兼容旧代码读路径，避免一次性重命名打断未迁移模块。
        return self.movie_info_translation

    @movie_desc_translation.setter
    def movie_desc_translation(self, value: MovieInfoTranslation) -> None:
        self.movie_info_translation = value

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
    # 运行时配置更新后，需要同时清理依赖配置的缓存单例。
    try:
        from src.service.discovery import get_image_search_service, get_lancedb_thumbnail_store
        from src.service.discovery.joytag_embedder_client import get_joytag_embedder_client

        get_image_search_service.cache_clear()
        get_lancedb_thumbnail_store.cache_clear()
        get_joytag_embedder_client.cache_clear()
    except Exception:
        pass


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
