from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.config.config import settings
from src.metadata.factory import refresh_gfriends_filetree
from src.plugins.contracts import PluginRegistration
from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins
from src.scheduler.ranking_plugin_adapter import apply_plugin_ranking_sources
from src.scheduler.contracts import JobDefinition
from src.scheduler.queue_tasks import QUEUE_TASK_REGISTRY
from src.service.catalog import (
    MovieCollectionService,
    MovieHeatService,
    MovieInteractionSyncService,
    SubscribedActorMovieSyncService,
)
from src.service.discovery import (
    DailyRecommendationService,
    HotReviewSyncService,
    ImageSearchIndexService,
    MomentRecommendationService,
    MovieRecommendationService,
)
from src.service.playback import (
    Cloud115KeepaliveService,
    MediaFileScanService,
    MediaThumbnailService,
)
from src.service.system import ActivityCleanupService, ResourceTaskAttemptCleanupService
from src.service.system.resource_task_runner import ResourceTaskLedger
from src.service.transfers.cloud115.offline.sync_service import (
    Cloud115OfflineSyncService,
)
from src.service.transfers.downloads.auto_subscribed.auto_download_service import (
    SubscribedMovieAutoDownloadService,
)
from src.service.transfers.downloads.small_file_cleanup_service import (
    DownloadSmallFileCleanupService,
)
from src.service.transfers.downloads.stalled_cleanup_service import (
    QBStalledCleanupService,
)
from src.service.transfers.downloads.sync_service import DownloadSyncService


def _build_stats_formatter(
    prefix: str,
    *names: str,
    **defaults: Any,
) -> Callable[[dict[str, Any]], str]:
    def _formatter(stats: dict[str, Any]) -> str:
        # 统一在这里处理缺省值（默认 0，个别字段经 **defaults 覆盖），
        # 避免注册表里散落大量重复的 s.get(...) 与 (name, name, 0) 三元组。
        formatted_fields = [
            f"{name}={stats.get(name, defaults.get(name, 0))}"
            for name in (*names, *defaults)
        ]
        return f"{prefix} {' '.join(formatted_fields)}"

    return _formatter


# ---------------------------------------------------------------------------
# 任务注册表
# ---------------------------------------------------------------------------

BUILTIN_JOB_REGISTRY: list[JobDefinition] = [
    JobDefinition(
        task_key="actor_subscription_sync",
        log_name="actor-subscription-sync",
        cli_name="sync-subscribed-actor-movies",
        cli_help="执行一次订阅女优影片抓取",
        cron_setting="actor_subscription_sync_cron",
        service_factory=lambda reporter: SubscribedActorMovieSyncService().sync_subscribed_actor_movies(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "sync finished:",
            "total_actors",
            "success_actors",
            "failed_actors",
            "imported_movies",
        ),
    ),
    JobDefinition(
        task_key="subscribed_movie_auto_download",
        log_name="subscribed-movie-auto-download",
        cli_name="auto-download-subscribed-movies",
        cli_help="执行一次已订阅缺失影片自动下载",
        cron_setting="subscribed_movie_auto_download_cron",
        # 已迁 kernel（Wave 2）：runner 直接使用 reporter.emit 上报进度。
        service_factory=lambda reporter: SubscribedMovieAutoDownloadService().run(reporter=reporter),
        business_recovery=lambda: {
            "recovered_running_movies": ResourceTaskLedger.recover_running(
                "subscribed_movie_auto_download",
                error_message="订阅影片资源查询任务中断，等待重试",
            )
        },
        format_stats=_build_stats_formatter(
            "auto download finished:",
            "candidate_movies",
            "searched_movies",
            "submitted_movies",
            "no_candidate_movies",
            "skipped_movies",
            "failed_movies",
        ),
    ),
    JobDefinition(
        task_key="movie_heat_update",
        log_name="movie-heat-update",
        cli_name="update-movie-heat",
        cli_help="执行一次影片热度重算",
        cron_setting="movie_heat_cron",
        service_factory=lambda _reporter: MovieHeatService.update_movie_heat(),
        format_stats=_build_stats_formatter(
            "heat update finished:",
            "candidate_count",
            "updated_count",
            formula_version="unknown",
        ),
    ),
    JobDefinition(
        task_key="movie_interaction_sync",
        log_name="movie-interaction-sync",
        cli_name="sync-movie-interactions",
        cli_help="执行一次影片互动数同步",
        cron_setting="movie_interaction_sync_cron",
        # 已迁 kernel（Wave 2）：runner 直接使用 reporter.emit 上报进度。
        service_factory=lambda reporter: MovieInteractionSyncService().run(reporter=reporter),
        business_recovery=lambda: {
            "recovered_running_movies": MovieInteractionSyncService.recover_interrupted_running_movies(
                error_message=MovieInteractionSyncService.INTERRUPTED_SYNC_ERROR_MESSAGE,
            )
        },
        format_stats=_build_stats_formatter(
            "movie interaction sync finished:",
            "candidate_movies",
            "processed_movies",
            "succeeded_movies",
            "failed_movies",
            "updated_movies",
            "unchanged_movies",
            "heat_updated_movies",
        ),
    ),
    JobDefinition(
        task_key="hot_review_sync",
        log_name="hot-review-sync",
        cli_name="sync-hot-reviews",
        cli_help="执行一次 JavDB 热评同步",
        cron_setting="hot_review_sync_cron",
        service_factory=lambda _reporter: HotReviewSyncService().sync_all_hot_reviews(),
        format_stats=_build_stats_formatter(
            "hot review sync finished:",
            "total_periods",
            "success_periods",
            "failed_periods",
            "fetched_reviews",
            "imported_movies",
            "skipped_reviews",
            "stored_items",
        ),
    ),
    JobDefinition(
        task_key="movie_collection_sync",
        log_name="movie-collection-sync",
        cli_name="sync-movie-collections",
        cli_help="执行一次合集影片标记同步",
        cron_setting="movie_collection_sync_cron",
        service_factory=lambda _reporter: MovieCollectionService.sync_movie_collections(),
        format_stats=_build_stats_formatter(
            "collection sync finished:",
            "total_movies",
            "matched_count",
            "updated_to_collection_count",
            "updated_to_single_count",
            "unchanged_count",
        ),
    ),
    JobDefinition(
        task_key="download_task_sync",
        log_name="download-task-sync",
        cli_name="sync-download-tasks",
        cli_help="执行一次下载任务状态同步",
        cron_setting="download_task_sync_cron",
        service_factory=lambda _reporter: DownloadSyncService().sync_all_clients(),
    ),
    JobDefinition(
        task_key="download_task_auto_import",
        log_name="download-task-auto-import",
        cli_name="auto-import-download-tasks",
        cli_help="执行一次已完成下载自动导入",
        cron_setting="download_task_auto_import_cron",
        service_factory=lambda _reporter: DownloadSyncService().enqueue_auto_imports(),
    ),
    JobDefinition(
        task_key="cloud115_offline_sync",
        log_name="cloud115-offline-sync",
        cli_name="sync-cloud115-offline-tasks",
        cli_help="执行一次 cloud115 离线任务对账（进度回写 / 完成导入 / 超时放弃）",
        cron_setting="cloud115_offline_sync_cron",
        service_factory=lambda _reporter: Cloud115OfflineSyncService().run(),
        format_stats=_build_stats_formatter(
            "cloud115 offline sync finished:",
            "total_clients",
            "updated_count",
            "import_triggered_count",
            "abandoned_count",
            "failed_count",
        ),
    ),
    JobDefinition(
        task_key="download_small_file_cleanup",
        log_name="download-small-file-cleanup",
        cli_name="cleanup-download-small-files",
        cli_help="执行一次下载中种子的小文件清理",
        cron_setting="download_small_file_cleanup_cron",
        service_factory=lambda _reporter: DownloadSmallFileCleanupService().cleanup_small_files(),
        format_stats=_build_stats_formatter(
            "download small file cleanup finished:",
            "total_clients",
            "scanned_torrents",
            "deselected_files",
            "deleted_files",
            "failed_count",
        ),
    ),
    JobDefinition(
        task_key="qb_stalled_cleanup",
        log_name="qb-stalled-cleanup",
        cli_name="cleanup-qb-stalled-tasks",
        cli_help="清理 qB 中长期停滞/龟速的下载任务（删种+删文件+拉黑）",
        cron_setting="qbittorrent_stalled_cleanup_cron",
        service_factory=lambda _reporter: QBStalledCleanupService().cleanup_stalled_tasks(),
        format_stats=_build_stats_formatter(
            "qb stalled cleanup finished:",
            "total_clients",
            "scanned_torrents",
            "cleaned_count",
            "failed_count",
        ),
    ),
    JobDefinition(
        task_key="media_file_scan",
        log_name="media-file-scan",
        cli_name="scan-media-files",
        cli_help="执行一次媒体文件巡检",
        cron_setting="media_file_scan_cron",
        service_factory=lambda reporter: MediaFileScanService().scan_media_files(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "media file scan finished:",
            "scanned_media",
            "updated_media",
            "skipped_media",
            "failed_media",
            "invalidated_media",
            "revived_media",
            # 远端清单枚举失败的 cloud115 库数：非 0 时该库媒体本轮未做 valid 判定，
            # 结果与"全部正常"同形，必须单独出数。
            "cloud115_index_failed_libraries",
        ),
    ),
    JobDefinition(
        task_key="media_thumbnail_generation",
        log_name="media-thumbnail-generation",
        cli_name="generate-media-thumbnails",
        cli_help="执行一次媒体缩略图生成",
        cron_setting="media_thumbnail_cron",
        # 已迁 kernel（Wave 2）：runner 直接使用 reporter.emit 上报进度。
        service_factory=lambda reporter: MediaThumbnailService.generate_pending_thumbnails(
            reporter=reporter,
        ),
        format_stats=_build_stats_formatter(
            "thumbnail generation finished:",
            "pending_media",
            "successful_media",
            "generated_thumbnails",
            "deferred_media",
            "retryable_failed_media",
            "terminal_failed_media",
            "exhausted_media",
        ),
    ),
    JobDefinition(
        task_key="image_search_index",
        log_name="image-search-index",
        cli_name="index-image-search-thumbnails",
        cli_help="执行一次以图搜图缩略图向量索引",
        cron_setting="image_search_index_cron",
        service_factory=lambda reporter: ImageSearchIndexService().index_pending_thumbnails(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "image search index finished:",
            "pending_thumbnails",
            "successful_thumbnails",
            "failed_thumbnails",
        ),
    ),
    JobDefinition(
        task_key="movie_similarity_recompute",
        log_name="movie-similarity-recompute",
        cli_name="recompute-movie-similarities",
        cli_help="执行一次影片相似度全量重算",
        cron_setting="movie_similarity_recompute_cron",
        service_factory=lambda reporter: MovieRecommendationService().recompute_all(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "movie similarity recompute finished:",
            "total_movies",
            "indexed_movies",
            "actor_features",
            "tag_features",
        ),
    ),
    JobDefinition(
        task_key="moment_recommendation_generate",
        log_name="moment-recommendation-generate",
        cli_name="generate-moment-recommendations",
        cli_help="执行一次推荐时刻生成",
        cron_setting="moment_recommendation_generate_cron",
        service_factory=lambda reporter: MomentRecommendationService().generate_recommendations(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "moment recommendation generate finished:",
            "seed_points",
            "visual_candidates",
            "similar_candidates",
            "popular_candidates",
            "stored_items",
        ),
    ),
    JobDefinition(
        task_key="daily_recommendation_generate",
        log_name="daily-recommendation-generate",
        cli_name="generate-daily-recommendations",
        cli_help="执行一次每日推荐快照生成",
        cron_setting="daily_recommendation_generate_cron",
        service_factory=lambda reporter: DailyRecommendationService.generate_latest_snapshot(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "daily recommendation generate finished:",
            "candidate_movies",
            "stored_items",
            cold_start=False,
            extreme_cold_start=False,
        ),
    ),
    JobDefinition(
        task_key="image_search_optimize",
        log_name="image-search-optimize",
        cli_name="optimize-image-search-index",
        cli_help="执行一次以图搜图向量索引优化",
        cron_setting="image_search_optimize_cron",
        service_factory=lambda _reporter: ImageSearchIndexService().optimize_index(),
        format_stats=_build_stats_formatter(
            "image search optimize finished:",
            optimized=False,
        ),
    ),
    JobDefinition(
        task_key="gfriends_filetree_refresh",
        log_name="gfriends-filetree-refresh",
        cli_name="refresh-gfriends-filetree",
        cli_help="拉取一次 GFriends Filetree 并写入本地缓存",
        cron_setting="gfriends_filetree_refresh_cron",
        service_factory=lambda _reporter: refresh_gfriends_filetree(force=True),
        format_stats=_build_stats_formatter(
            "gfriends filetree refresh finished:",
            "entries",
            "bytes_written",
            source="unknown",
        ),
    ),
    JobDefinition(
        task_key="activity_record_cleanup",
        log_name="activity-record-cleanup",
        cli_name="cleanup-activity-records",
        cli_help="执行一次活动中心记录清理（事件流 / 任务运行 / 已读通知）",
        cron_setting="activity_cleanup_cron",
        service_factory=lambda _reporter: ActivityCleanupService().cleanup(),
        format_stats=_build_stats_formatter(
            "activity record cleanup finished:",
            "deleted_events",
            "deleted_task_runs",
            "deleted_notifications",
        ),
    ),
    JobDefinition(
        task_key="resource_task_attempt_cleanup",
        log_name="resource-task-attempt-cleanup",
        cli_name="cleanup-resource-task-attempts",
        cli_help="执行一次资源任务尝试历史保留期清理",
        cron_setting="resource_task_attempt_cleanup_cron",
        service_factory=lambda _reporter: ResourceTaskAttemptCleanupService().cleanup(),
        format_stats=_build_stats_formatter(
            "resource task attempt cleanup finished:",
            "deleted_attempts",
        ),
    ),
    JobDefinition(
        task_key="cloud115_cookies_keepalive",
        log_name="cloud115-cookies-keepalive",
        cli_name="keepalive-cloud115-cookies",
        cli_help="执行一次 cloud115 库 cookies 探活与快照回写",
        cron_setting="cloud115_keepalive_cron",
        service_factory=lambda _reporter: Cloud115KeepaliveService().run(),
        format_stats=_build_stats_formatter(
            "cloud115 keepalive finished:",
            "total",
            "alive",
            "expired",
            "unavailable",
        ),
    ),
]


def _build_job_registry(
    builtin_jobs: list[JobDefinition],
    plugins: tuple[PluginRegistration, ...],
) -> list[JobDefinition]:
    """先校验全部唯一标识，再发布完整注册表。

    内建任务冲突属开发错误，直接抛；插件任务冲突记入 PLUGIN_LOAD_ERRORS
    并跳过该插件，保证坏插件不会拖垮服务启动。
    """
    jobs = [*builtin_jobs]
    for plugin in plugins:
        jobs.extend(plugin.jobs)

    queue_task_keys = set(QUEUE_TASK_REGISTRY)
    rejected_plugins: set[str] = set()

    # 三字段唯一性校验：内建与内建冲突是开发错误直接抛；
    # 插件参与的冲突一律隔离插件，保证坏插件不会拖垮服务启动。
    for field_name in ("task_key", "cli_name", "log_name"):
        owners: dict[str, str] = {}
        for job in jobs:
            if job.plugin_id in rejected_plugins:
                continue
            value = getattr(job, field_name)
            owner = job.plugin_id or "core"
            previous_owner = owners.get(value)
            if previous_owner is not None:
                if owner == "core" and previous_owner == "core":
                    raise RuntimeError(
                        f"任务注册冲突 field={field_name} value={value} "
                        f"owners={previous_owner},{owner}"
                    )
                # 插件与内建/插件冲突：内建任务必须保留，隔离冲突的插件。
                offender = owner if owner != "core" else previous_owner
                PLUGIN_LOAD_ERRORS[offender] = {
                    "stage": "registry_conflict",
                    "message": (
                        f"任务注册冲突 field={field_name} value={value} "
                        f"owners={previous_owner},{owner}"
                    ),
                }
                rejected_plugins.add(offender)
                continue
            owners[value] = owner

    # 队列专属 key 冲突同样只隔离插件（内建与队列同 key 是设计内语义）。
    result: list[JobDefinition] = [*builtin_jobs]
    for plugin in plugins:
        if plugin.plugin_id in rejected_plugins:
            continue
        for job in plugin.jobs:
            if job.task_key in queue_task_keys:
                PLUGIN_LOAD_ERRORS[plugin.plugin_id] = {
                    "stage": "registry_conflict",
                    "message": (
                        f"任务注册冲突 field=task_key value={job.task_key} "
                        f"owners={plugin.plugin_id},queue"
                    ),
                }
                rejected_plugins.add(plugin.plugin_id)
                break
        if plugin.plugin_id not in rejected_plugins:
            result.extend(plugin.jobs)
    return result


# 显式启用插件在 import 阶段完整加载；单个插件失败记入 PLUGIN_LOAD_ERRORS 并隔离。
LOADED_PLUGINS: tuple[PluginRegistration, ...] = load_enabled_plugins(
    settings.plugins,
    root_dir=settings.plugins.root_dir,
)
# 排行榜来源合并：来源冲突的插件整插件隔离（任务也不注册）。
_REJECTED_RANKING_PLUGINS: set[str] = apply_plugin_ranking_sources(LOADED_PLUGINS)
_ACTIVE_PLUGINS: tuple[PluginRegistration, ...] = tuple(
    plugin
    for plugin in LOADED_PLUGINS
    if plugin.plugin_id not in _REJECTED_RANKING_PLUGINS
)
JOB_REGISTRY: list[JobDefinition] = _build_job_registry(
    BUILTIN_JOB_REGISTRY,
    _ACTIVE_PLUGINS,
)
JOB_REGISTRY_BY_KEY: dict[str, JobDefinition] = {job.task_key: job for job in JOB_REGISTRY}
