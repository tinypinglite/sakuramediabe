import json
import math
import os
import pathlib
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlparse

import toml
from apscheduler.triggers.cron import CronTrigger
from loguru import logger
from pydantic import (
    BaseModel,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from src.plugins.manifest import PLUGIN_ID_PATTERN


# 这里处理的是番号**前缀**（如 OFJE），不是完整番号，所以只做去空白 + 大写：
# 前缀要跟已归一化的番号做 startswith 比较，大写是必须的；而 normalize_movie_number 的
# _→- 与抹 PPV- 是番号整体的形状规则，对前缀无意义（抹 PPV- 更会把 PPV-XXX 削成 XXX）。
# 两者是不同数据类型的不同规则，不要在这里去复用番号归一化。
def _normalize_number_feature(value: str) -> str:
    return (value or "").strip().upper()


# 校验分档：
# - 无 context（默认，覆盖启动期 Settings() 从 toml 加载）：仅 warn 保留原值，避免存量非法配置让进程启动即崩。
# - context={"strict": True}（覆盖配置 API 写入路径）：直接 raise，阻止把非法值写入磁盘。
def _validation_is_strict(info: ValidationInfo | None) -> bool:
    if info is None:
        return False
    context = info.context
    if isinstance(context, dict):
        return bool(context.get("strict"))
    return False


def _check_http_url(value: str, label: str, info: ValidationInfo | None) -> str:
    parsed = urlparse((value or "").strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    message = f"{label} 必须是 http 或 https URL"
    if _validation_is_strict(info):
        raise ValueError(message)
    logger.warning("配置 {} 不是合法的 http/https URL（当前值={!r}），将原样保留使用", label, value)
    return value


class DatabaseEngine(str, Enum):
    POSTGRES = "postgres"


class IndexerKind(str, Enum):
    PT = "pt"
    BT = "bt"


class Database(BaseModel):
    engine: DatabaseEngine = DatabaseEngine.POSTGRES
    url: str = "postgresql://sakuramedia:sakuramedia@postgres:5432/sakuramedia"


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
    allowed_min_video_file_size: int = 268435456 # 256MB
    import_image_root_path: str = "/data/cache/assets"
    # legacy：字幕已统一到 movies/<shard>/<番号>/subtitles/，此项仅供 migrate-movie-subtitles
    # CLI 定位旧字幕根，运行期不再有任何写入。存量搬完后可以从配置里删掉。
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

    @field_validator("others_number_features", mode="before")
    @classmethod
    def _normalize_others_number_features(cls, value, info: ValidationInfo):
        # 规范化：对每一项按番号习惯归一（去空白 / 大写 / _→- / 抹 PPV-）；空结果按分档处理：
        # - 严格档（配置 API 写入）：抛错，阻止把空/纯空白项写入磁盘
        # - 宽松档（启动加载）：静默丢弃，兼容存量脏值
        # 保持返回 set[str]，避免上游 movie_collection_service 等按集合语义比较的地方被打破。
        if value is None:
            return set()
        strict = _validation_is_strict(info)
        normalized: set[str] = set()
        for item in value:
            result = _normalize_number_feature(str(item))
            if result:
                normalized.add(result)
            elif strict:
                raise ValueError(f"invalid number feature: {item!r}")
        return normalized


class Metadata(BaseModel):
    # 不再提供显式代理配置：所有外部站点请求统一跟随容器环境变量
    # HTTP_PROXY / HTTPS_PROXY / NO_PROXY 分流（httpx trust_env 默认开启）。
    javdb_host: str = "jdforrepam.com"
    gfriends_filetree_url: str = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/Filetree.json"
    gfriends_cdn_base_url: str = "https://cdn.jsdelivr.net/gh/xinxin8816/gfriends"
    gfriends_filetree_cache_path: str = "/data/cache/gfriends/gfriends-filetree.json"
    gfriends_filetree_cache_ttl_hours: int = 24 * 7
    import_metadata_max_workers: int = 3

    @field_validator("gfriends_filetree_url", "gfriends_cdn_base_url")
    @classmethod
    def _check_gfriends_urls(cls, value: str, info: ValidationInfo) -> str:
        return _check_http_url(value, "gfriends URL", info)



_PLUGIN_ID_PATTERN = PLUGIN_ID_PATTERN


class Plugins(BaseModel):
    """仓库内可信插件配置；插件必须出现在 enabled 中才会被导入。"""

    # 插件根目录：生产默认 /data/plugins；本地开发可指向 ./storage/plugins。
    root_dir: str = "/data/plugins"
    enabled: list[str] = Field(default_factory=list)
    job_crons: dict[str, dict[str, str]] = Field(default_factory=dict)
    settings: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("enabled")
    @classmethod
    def _validate_enabled_plugin_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("plugins.enabled 不允许包含重复插件 ID")
        for plugin_id in value:
            if not _PLUGIN_ID_PATTERN.fullmatch(plugin_id):
                raise ValueError(
                    f"插件 ID 只能包含小写字母、数字、下划线且必须以字母开头: {plugin_id}"
                )
        return value

    @model_validator(mode="after")
    def _validate_plugin_config_namespaces(self):
        # 未启用插件允许保留配置，但配置命名空间本身仍必须是合法插件 ID。
        for section_name, section in (
            ("job_crons", self.job_crons),
            ("settings", self.settings),
        ):
            for plugin_id in section:
                if not _PLUGIN_ID_PATTERN.fullmatch(plugin_id):
                    raise ValueError(
                        f"plugins.{section_name} 包含非法插件 ID: {plugin_id}"
                    )
        return self


class Scheduler(BaseModel):
    enabled: bool = True
    log_dir: str = "/data/logs"
    actor_subscription_sync_cron: str = "0 2 * * *"
    subscribed_movie_auto_download_cron: str = "30 2 * * *"
    download_task_sync_cron: str = "*/5 * * * *"
    # 宿主内部 qB 进度快照采样周期；不进入任务注册表，也不创建 TaskRun。
    download_progress_snapshot_interval_seconds: float = Field(default=20.0, ge=1.0, le=60.0)
    download_task_auto_import_cron: str = "*/10 * * * *"
    download_small_file_cleanup_cron: str = "*/5 * * * *"
    # qB 停滞/慢速任务清理：每天凌晨 1 点跑（早于订阅自动下载 02:30，删完当天就能换种重下）。
    qbittorrent_stalled_cleanup_cron: str = "0 1 * * *"
    # cloud115 离线任务对账：远端进度回写 + 完成触发导入 + 超时放弃。cron 最小粒度即 1 分钟；
    # 没有活跃任务时对账是零请求空转，不会打扰 115。
    cloud115_offline_sync_cron: str = "* * * * *"
    movie_collection_sync_cron: str = "0 1 * * *"
    movie_heat_cron: str = "15 0 * * *"
    movie_interaction_sync_cron: str = "0 5 * * *"
    hot_review_sync_cron: str = "20 1 * * *"
    # 全量巡检会 stat 媒体库里每个文件，放到每天凌晨集中一次，避免高频唤醒媒体盘。
    media_file_scan_cron: str = "0 4 * * *"
    # 空跑只查 DB 不读盘，30 分钟一次足够；有新导入时缩略图会在同一活跃窗口内跟上。
    media_thumbnail_cron: str = "*/30 * * * *"
    image_search_index_cron: str = "0 0 * * *"
    plot_image_search_index_cron: str = "30 0 * * *"
    image_search_optimize_cron: str = "0 3 * * *"
    movie_similarity_recompute_cron: str = "30 3 * * *"
    moment_recommendation_generate_cron: str = "0 4 * * *"
    daily_recommendation_generate_cron: str = "0 5 * * *"
    activity_cleanup_cron: str = "30 5 * * *"
    # cloud115 cookies 保活：acw_tc（阿里云 WAF token）30 分钟过期，每 20 分钟探活一次
    # 并把 SDK merge 到的最新快照回写库配置；长效凭据失效时发通知引导重新扫码。
    cloud115_keepalive_cron: str = "*/20 * * * *"
    # GFriends Filetree 缓存刷新：默认每周一 04:00，对齐 disk cache 默认 7 天 TTL。
    gfriends_filetree_refresh_cron: str = "0 4 * * 1"
    # 活动中心保留期：每个 task_key 只保留最近 N 条运行记录，已读通知保留最近 N 天。
    # 具体语义见 ActivityCleanupService。
    activity_task_run_retention_per_key: int = 200
    activity_notification_read_retention_days: int = 3

    @model_validator(mode="before")
    @classmethod
    def _validate_cron_expressions(cls, data, info: ValidationInfo):
        # 严格档（配置 API 写入）遇非法 cron 直接拒；宽松档（启动加载）仅 warn，避免存量非法 cron 让进程启动即崩，
        # 由 aps 进程装配时再报错更定位得到。
        if not isinstance(data, dict):
            return data
        strict = _validation_is_strict(info)
        for name, value in data.items():
            if not (isinstance(name, str) and name.endswith("_cron") and isinstance(value, str)):
                continue
            try:
                CronTrigger.from_crontab(value)
            except (ValueError, TypeError) as exc:
                if strict:
                    raise ValueError(f"{name} 不是合法的 cron 表达式: {value}") from exc
                logger.warning(
                    "scheduler.{} 不是合法的 cron 表达式（当前值={!r}）：{}；将原样保留，实际调度时可能失败",
                    name, value, exc,
                )
        return data



class Downloads(BaseModel):
    # 下载中种子内小于该大小（MB）的文件视为可清理小文件，会被设为不下载并物理删除。
    small_file_cleanup_threshold_mb: int = 256
    # 下载入口 kind 的全局偏好顺序：索引器绑定多个下载器时按此顺序挑选，列表外的 kind 排最后。
    # 只影响挑选顺序，不做白名单；选中的下载器执行失败时直接报错，不自动换下一个。
    preferred_client_kinds: list[str] = Field(default_factory=lambda: ["qbittorrent", "cloud115"])
    # cloud115 离线任务超过该时长仍未完成即本地放弃：不清理 115 侧任务、停止轮询并通知用户。
    cloud115_offline_abandon_hours: int = Field(default=24, ge=1)
    # 批量秒传对 115 webapi 的全局请求限速：相邻请求最小间隔（秒）。webapi 前置阿里云 WAF
    # 约 1-2 r/s 阈值，默认 1.0（=1 r/s，对标 AList 115 驱动 limit_rate）。0 关闭限速。
    cloud115_rapid_upload_min_interval_seconds: float = Field(default=1.0, ge=0.0, le=10.0)
    # 批量链路（导入 / 秒传 / 巡检）的累计请求节奏：每打满 N 个 webapi 请求就长休一次。
    # 匀速的 min_interval 管得住瞬时速率，管不住"一个任务累计打几百个请求"——实测连续
    # 200 余次 GET /files（1 r/s）即触发 WAF 405。只对 webapi.115.com 计数，取直链/离线/
    # 探活不在风控域。0 = 关闭批次休息。交互路径（播放、GUI 浏览）永不启用，避免用户干等。
    cloud115_batch_rest_every_requests: int = Field(default=30, ge=0)
    cloud115_batch_rest_min_seconds: float = Field(default=10.0, ge=0.0, le=300.0)
    cloud115_batch_rest_max_seconds: float = Field(default=30.0, ge=0.0, le=300.0)
    # qBittorrent 任务停在 stalledDL（下载中无源），且 qB 报告的 last_activity（最后一次收发
    # chunk 的时刻）已早于该时长，即判定为死种（对账时落成 download_state=stalled_dead）：
    # 释放该影片重新参与订阅资源查询，并把这个 info_hash 加入该影片的选种黑名单。
    # 比 115 的 cloud115_offline_abandon_hours 宽松得多——115 是云端拉取，24h 拉不到基本就是没有；
    # 本地 BT 依赖 peer 在线，老片常态是只有一两个 seeder 且不挂机，卡几天后复活很正常。
    qbittorrent_stalled_abandon_days: int = Field(default=7, ge=1)
    # qB 停滞/慢速任务自动清理：处于活跃下载态（stalled/downloading，由对账维护
    # download_started_at 计时，排队时长不计）且未完成、超过该时长仍未下完的种子，
    # 直接删种并连带删除已下载文件，本地行落 stalled_dead 拉黑（同 info_hash 不再自动提交，
    # 影片换其他候选重下）。queuedDL（排队）/ pausedDL / stoppedDL 永不自动清理。
    # 这是破坏性动作（删文件不可恢复）：判定完全依赖系统侧维护的开始时刻，首次部署首轮
    # 只写入不删除；存量行 download_started_at 为空时先让对账起算。enabled=False 关闭该清理。
    qbittorrent_stalled_cleanup_enabled: bool = Field(default=True)
    qbittorrent_stalled_cleanup_hours: int = Field(default=24, ge=1)
    # 新片持续查询，老片连续未找到达到上限后进入 exhausted，等待用户显式重开。
    subscription_search_fresh_days: int = Field(default=90, ge=1)
    subscription_search_stale_attempt_limit: int = Field(default=3, ge=1)


class MediaImport(BaseModel):
    # 可视化导入只允许浏览/导入这些白名单根目录（含其子树），其余路径一律 403。
    # 采用白名单而非黑名单，避免暴露应用配置、数据库、家目录等敏感路径。
    browse_roots: list[str] = Field(default_factory=lambda: ["/mnt"])


class Logging(BaseModel):
    level: str = "INFO"


class ImageSearch(BaseModel):
    inference_base_url: str = "http://joytag-infer:8001"
    # CPU 后端逐张推理，一批 16 张会串行跑满 16 次；30s 不足以覆盖，中途超时会让整批作废。
    inference_timeout_seconds: float = 120.0
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

    @field_validator("inference_base_url")
    @classmethod
    def _check_inference_base_url(cls, value: str, info: ValidationInfo) -> str:
        return _check_http_url(value, "inference_base_url", info)


class Qdrant(BaseModel):
    url: str = "http://qdrant:6333"
    api_key: str = ""

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str, info: ValidationInfo) -> str:
        return _check_http_url(value, "qdrant.url", info)


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
    metadata: Metadata = Field(default_factory=Metadata)
    plugins: Plugins = Field(default_factory=Plugins)
    scheduler: Scheduler = Field(default_factory=Scheduler)
    downloads: Downloads = Field(default_factory=Downloads)
    media_import: MediaImport = Field(default_factory=MediaImport)
    logging: Logging = Field(default_factory=Logging)
    image_search: ImageSearch = Field(default_factory=ImageSearch)
    qdrant: Qdrant = Field(default_factory=Qdrant)
    enable_docs: bool = False

    model_config = SettingsConfigDict(
        toml_file=SETTINGS_TOML_PATH,
        extra="ignore",
        env_nested_delimiter="__",
    )

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_settings(cls, data: Any):
        if not isinstance(data, dict):
            return data
        normalized_data = dict(data)
        # 兼容历史遗留的媒体音频识别配置节，读取时直接忽略，避免旧 config.toml 导致启动失败。
        normalized_data.pop("media_asr", None)
        return normalized_data

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def get_settings() -> Settings:
    return Settings()


settings = Settings()
_SETTINGS_WRITE_LOCK = RLock()


@contextmanager
def settings_write_lock() -> Iterator[None]:
    """串行化进程内的配置读改写，避免局部 PATCH 相互覆盖。"""
    with _SETTINGS_WRITE_LOCK:
        yield


def load_persisted_settings() -> Settings:
    """从当前 TOML 创建配置快照；读改写调用方须持有 settings_write_lock。"""
    return Settings()


def refresh_runtime_settings(new_settings: Settings) -> None:
    for field_name in Settings.model_fields:
        setattr(settings, field_name, getattr(new_settings, field_name))
    # 运行时配置更新后，需要同时清理依赖配置的缓存单例。
    try:
        from src.service.discovery import (
            get_image_search_service,
            get_movie_plot_image_search_service,
            get_qdrant_plot_image_store,
            get_qdrant_thumbnail_store,
        )
        from src.service.discovery.joytag_embedder_client import (
            get_joytag_embedder_client,
        )

        get_image_search_service.cache_clear()
        get_movie_plot_image_search_service.cache_clear()
        get_qdrant_plot_image_store.cache_clear()
        get_qdrant_thumbnail_store.cache_clear()
        get_joytag_embedder_client.cache_clear()
    except Exception:
        pass


def _build_persistable_settings(settings_to_persist: Settings) -> dict[str, Any]:
    # file_signature_secret 现在是首启自举的持久化字段，正常写盘，不再排除。
    return json.loads(settings_to_persist.model_dump_json())


def persist_settings(new_settings: Settings) -> bool:
    """原子写入配置文件，不改变当前进程的 settings 快照。"""
    with settings_write_lock():
        serializable_settings = _build_persistable_settings(new_settings)
        settings_path = Path(Settings.model_config["toml_file"])
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(settings_path.stat().st_mode) if settings_path.exists() else None
        temporary_path: Path | None = None
        try:
            # 同目录临时文件完成落盘后再 replace，避免进程重启时读到半截 TOML。
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=settings_path.parent,
                prefix=f".{settings_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temporary_path = Path(file.name)
                file.write(toml.dumps(serializable_settings))
                file.flush()
                os.fsync(file.fileno())
            if mode is not None:
                temporary_path.chmod(mode)
            os.replace(temporary_path, settings_path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
    return True


def update_settings(new_settings: Settings) -> bool:
    """兼容插件管理器：只更新 plugins 段，并同步当前进程的插件配置。"""
    with settings_write_lock():
        # 插件管理器持有的 settings 可能早于普通配置 PATCH；以磁盘为底只覆盖 plugins，
        # 防止安装/配置插件时把尚未重启的普通配置写回旧值。
        persisted_settings = load_persisted_settings()
        persisted_settings.plugins = new_settings.plugins.model_copy(deep=True)
        persist_settings(persisted_settings)

        # 保持普通配置的运行时旧快照，仅让插件配置沿用原有即时可见行为。
        runtime_settings = Settings.model_validate(settings.model_dump())
        runtime_settings.plugins = new_settings.plugins.model_copy(deep=True)
        refresh_runtime_settings(runtime_settings)
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
