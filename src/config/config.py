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
    # 空字符串作为“未初始化”哨兵：首次启动由 ensure_runtime_secrets() 生成随机值并落盘。
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 30
    refresh_token_expire_minutes: int = 60 * 24 * 7
    # 同样首启自举并持久化；不再每次启动随机生成，避免重启后既有签名 URL 全部失效。
    file_signature_secret: str = ""


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
    # 片段产物独立存储根目录，部署时单独挂卷映射到本地。
    media_clip_root_path: str = "/data/media-clips"
    # 用户可圈选的片段最大时长（秒），仅约束区间长度，不等于 ffmpeg 进程墙钟时长。
    media_clip_max_duration_seconds: int = 900
    # 单次 ffmpeg 切片的墙钟超时（秒）：兜住坏文件/慢挂载导致的进程卡死，超时即杀进程。
    media_clip_ffmpeg_timeout_seconds: int = 120


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
    javdb_host: str = "jdforrepam.com"
    proxy: str | None = None
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
    download_small_file_cleanup_cron: str = "*/5 * * * *"
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
    moment_recommendation_generate_cron: str = "0 4 * * *"
    daily_recommendation_generate_cron: str = "0 5 * * *"
    activity_cleanup_cron: str = "30 5 * * *"
    # 活动中心三张表的保留期：事件流只保留最近 N 天，每个 task_key 只保留最近 N 条运行记录，
    # 已读通知保留最近 N 天。具体语义见 ActivityCleanupService。
    activity_event_retention_days: int = 1
    activity_task_run_retention_per_key: int = 200
    activity_notification_read_retention_days: int = 3



class Downloads(BaseModel):
    # 下载中种子内小于该大小（MB）的文件视为可清理小文件，会被设为不下载并物理删除。
    small_file_cleanup_threshold_mb: int = 256


class MediaImport(BaseModel):
    # 可视化导入只允许浏览/导入这些白名单根目录（含其子树），其余路径一律 403。
    # 采用白名单而非黑名单，避免暴露应用配置、数据库、家目录等敏感路径。
    browse_roots: list[str] = Field(default_factory=lambda: ["/mnt"])


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


class Qdrant(BaseModel):
    url: str = "http://qdrant:6333"
    api_key: str = ""


_DATA_CONFIG_PATH = Path('/data/config/config.toml')
if _DATA_CONFIG_PATH.exists():
    SETTINGS_TOML_PATH = _DATA_CONFIG_PATH
elif Path('/data').is_dir():
    # 容器内：/data 已挂载（entrypoint 会 mkdir -p /data/config）但配置文件缺失，
    # 仍以 /data/config/config.toml 为目标——缺文件时走纯默认值，再由 ensure_runtime_secrets() 自举写入。
    # 不回退到仓库内 config.toml，避免容器误读开发者本地配置。
    logger.warning("No config.toml at /data/config/config.toml; will bootstrap defaults and write secrets there.")
    SETTINGS_TOML_PATH = _DATA_CONFIG_PATH
else:
    # 本地开发：无 /data 目录，回退到仓库内 config.toml。
    logger.warning("No /data directory found, using repository config.toml path.")
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
    downloads: Downloads = Field(default_factory=Downloads)
    media_import: MediaImport = Field(default_factory=MediaImport)
    logging: Logging = Field(default_factory=Logging)
    indexer_settings: IndexerSettings = Field(default_factory=IndexerSettings)
    image_search: ImageSearch = Field(default_factory=ImageSearch)
    qdrant: Qdrant = Field(default_factory=Qdrant)
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
        from src.service.discovery import get_image_search_service, get_qdrant_thumbnail_store
        from src.service.discovery.joytag_embedder_client import get_joytag_embedder_client

        get_image_search_service.cache_clear()
        get_qdrant_thumbnail_store.cache_clear()
        get_joytag_embedder_client.cache_clear()
    except Exception:
        pass


def _build_persistable_settings(settings_to_persist: Settings) -> dict[str, Any]:
    # file_signature_secret 现在是首启自举的持久化字段，正常写盘，不再排除。
    return json.loads(settings_to_persist.model_dump_json())


def update_settings(new_settings: Settings) -> bool:
    serializable_settings = _build_persistable_settings(new_settings)
    settings_path = Path(Settings.model_config["toml_file"])
    with open(settings_path, "w", encoding="utf-8") as file:
        file.write(toml.dumps(serializable_settings))
    refresh_runtime_settings(new_settings)
    return True


# 视为“未初始化/不安全”的 secret_key 值：空、示例占位、历史硬编码默认值，命中即重新生成。
_INSECURE_SECRET_KEYS = {"", "replace-with-a-random-secret-key", "98765432178965437"}


def _ensure_auth_secrets() -> dict[str, str]:
    """把缺失/不安全的鉴权密钥生成随机值并写回内存全局 settings，返回本次生成的字段。"""
    updates: dict[str, str] = {}
    if settings.auth.secret_key in _INSECURE_SECRET_KEYS:
        updates["secret_key"] = secrets.token_urlsafe(48)
    if not settings.auth.file_signature_secret:
        updates["file_signature_secret"] = secrets.token_urlsafe(32)
    for field_name, value in updates.items():
        setattr(settings.auth, field_name, value)
    return updates


def ensure_runtime_config() -> bool:
    """首次启动自举运行配置。

    - 始终先确保鉴权密钥就绪（secret_key 空/占位/旧硬编码、file_signature_secret 为空时生成随机值），
      并写回内存全局 settings。
    - 目标 config.toml 缺失或为空时，写入一份含全部配置项默认值（含已生成密钥）的完整文件。
    - 目标 config.toml 已有内容时，仅以“只补 [auth] 两键”的方式 surgical 持久化密钥，保留其余配置。
    仅当确有写盘时返回 True，幂等。
    """
    secret_updates = _ensure_auth_secrets()

    settings_path = Path(Settings.model_config["toml_file"])
    # 文件缺失或内容为空白，都视为需要写入一份完整默认配置。
    file_missing_or_empty = (
        not settings_path.exists()
        or not settings_path.read_text(encoding="utf-8").strip()
    )

    if file_missing_or_empty:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        # 目标文件缺失/为空时写入全量默认配置：以 Settings()（此刻读取缺失/空源即得默认值）为基准，
        # 叠加已生成的密钥后整体落盘，与运行进程内存中的密钥保持一致。
        default_settings = Settings()
        default_settings.auth.secret_key = settings.auth.secret_key
        default_settings.auth.file_signature_secret = settings.auth.file_signature_secret
        serializable_settings = _build_persistable_settings(default_settings)
        with open(settings_path, "w", encoding="utf-8") as file:
            file.write(toml.dumps(serializable_settings))
        logger.info("Bootstrapped full default config with generated secrets at {}", settings_path)
        return True

    # 文件已有内容：仅在密钥有变更时 surgical 落盘，避免 model_dump 丢弃模型外字段。
    if not secret_updates:
        return False
    existing_config: dict[str, Any] = toml.load(settings_path)
    existing_config.setdefault("auth", {}).update(secret_updates)
    with open(settings_path, "w", encoding="utf-8") as file:
        file.write(toml.dumps(existing_config))
    logger.info("Persisted generated auth secrets: {}", ", ".join(sorted(secret_updates)))
    return True
