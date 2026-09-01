from __future__ import annotations

from src.config.config import settings
from src.plugins.contracts import PluginRegistration
from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins
from src.plugins.provider_protocol import refresh_media_provider_registry
from src.scheduler.contracts import JobDefinition
from src.scheduler.queue_tasks import (
    QUEUE_TASK_REGISTRY,
    _run_gfriends_filetree_refresh,
)
from src.scheduler.ranking_plugin_adapter import apply_plugin_ranking_sources
from src.service.catalog import (
    MovieHeatService,
    MovieInteractionSyncService,
    MovieTaskService,
    SubscribedActorMovieSyncService,
)
from src.service.catalog.movie_subscription_search_state_service import (
    MovieSubscriptionSearchStateService,
)
from src.service.discovery import (
    DailyRecommendationService,
    ImageSearchIndexService,
    MomentRecommendationService,
    MovieRecommendationService,
)
from src.service.playback import (
    MediaDurationBackfillService,
    MediaFileHashBackfillService,
    MediaThumbnailService,
)
from src.service.system import ActivityCleanupService
from src.service.transfers.downloads.auto_subscribed.auto_download_service import (
    SubscribedMovieAutoDownloadService,
)
from src.service.transfers.downloads.sync_service import DownloadSyncService

# ---------------------------------------------------------------------------
# 任务注册表
# ---------------------------------------------------------------------------


def _run_movie_heat(reporter, params):
    return (
        MovieTaskService.execute_movie_heat(reporter, params)
        if params
        else MovieHeatService.update_movie_heat()
    )

BUILTIN_JOB_REGISTRY: list[JobDefinition] = [
    JobDefinition(
        task_key="actor_subscription_sync",
        log_name="actor-subscription-sync",
        cli_name="sync-subscribed-actor-movies",
        cli_help="执行一次订阅女优影片抓取",
        cron_setting="actor_subscription_sync_cron",
        handler=lambda reporter, _params: SubscribedActorMovieSyncService().sync_subscribed_actor_movies(
            progress_callback=reporter.progress_callback,
        ),
    ),
    JobDefinition(
        task_key="subscribed_movie_auto_download",
        log_name="subscribed-movie-auto-download",
        cli_name="auto-download-subscribed-movies",
        cli_help="执行一次已订阅缺失影片自动下载",
        cron_setting="subscribed_movie_auto_download_cron",
        handler=lambda reporter, _params: SubscribedMovieAutoDownloadService().run(reporter=reporter),
        business_recovery=lambda: {
            "recovered_running_movies": (
                MovieSubscriptionSearchStateService.recover_interrupted_running_movies()
            )
        },
    ),
    JobDefinition(
        task_key="movie_heat_update",
        log_name="movie-heat-update",
        cli_name="update-movie-heat",
        cli_help="执行一次影片热度重算",
        cron_setting="movie_heat_cron",
        handler=_run_movie_heat,
    ),
    JobDefinition(
        task_key="movie_interaction_sync",
        log_name="movie-interaction-sync",
        cli_name="sync-movie-interactions",
        cli_help="执行一次影片互动数同步",
        cron_setting="movie_interaction_sync_cron",
        handler=lambda reporter, _params: MovieInteractionSyncService().run(reporter=reporter),
    ),
    JobDefinition(
        task_key="download_task_sync",
        log_name="download-task-sync",
        cli_name="sync-download-tasks",
        cli_help="执行一次下载任务状态同步",
        cron_setting="download_task_sync_cron",
        handler=lambda _reporter, _params: DownloadSyncService().sync_all_clients(),
    ),
    JobDefinition(
        task_key="download_task_auto_import",
        log_name="download-task-auto-import",
        cli_name="auto-import-download-tasks",
        cli_help="执行一次已完成下载自动导入",
        cron_setting="download_task_auto_import_cron",
        handler=lambda _reporter, _params: DownloadSyncService().enqueue_auto_imports(),
    ),
    JobDefinition(
        task_key=MediaFileHashBackfillService.TASK_KEY,
        log_name="media-file-hash-backfill",
        cli_name="backfill-media-file-hashes",
        cli_help="执行一次空媒体文件哈希补算",
        cron_setting="media_file_hash_backfill_cron",
        handler=lambda reporter, _params: MediaFileHashBackfillService.backfill_missing_file_hashes(
            reporter=reporter,
        ),
    ),
    JobDefinition(
        task_key=MediaDurationBackfillService.TASK_KEY,
        log_name="media-duration-backfill",
        cli_name="backfill-media-durations",
        cli_help="补齐有效媒体缺失的时长",
        manual_only=True,
        handler=lambda reporter, _params: MediaDurationBackfillService.backfill_missing_durations(
            reporter=reporter,
        ),
    ),
    JobDefinition(
        task_key="media_thumbnail_generation",
        log_name="media-thumbnail-generation",
        cli_name="generate-media-thumbnails",
        cli_help="执行一次媒体缩略图生成",
        cron_setting="media_thumbnail_cron",
        handler=lambda reporter, _params: MediaThumbnailService.generate_pending_thumbnails(
            reporter=reporter,
        ),
    ),
    JobDefinition(
        task_key="image_search_index",
        log_name="image-search-index",
        cli_name="index-image-search",
        cli_help="持续构建缩略图和剧情图的搜索向量索引，直到待处理队列为空",
        cron_setting="image_search_index_cron",
        handler=lambda reporter, params: ImageSearchIndexService().index_pending_images(
            progress_callback=reporter.progress_callback,
            reset=params.get("reset") is True,
        ),
    ),
    JobDefinition(
        task_key="movie_similarity_recompute",
        log_name="movie-similarity-recompute",
        cli_name="recompute-movie-similarities",
        cli_help="执行一次影片相似度全量重算",
        cron_setting="movie_similarity_recompute_cron",
        handler=lambda reporter, _params: MovieRecommendationService().recompute_all(
            progress_callback=reporter.progress_callback,
        ),
    ),
    JobDefinition(
        task_key="moment_recommendation_generate",
        log_name="moment-recommendation-generate",
        cli_name="generate-moment-recommendations",
        cli_help="执行一次推荐时刻生成",
        cron_setting="moment_recommendation_generate_cron",
        handler=lambda reporter, _params: MomentRecommendationService().generate_recommendations(
            progress_callback=reporter.progress_callback,
        ),
    ),
    JobDefinition(
        task_key="daily_recommendation_generate",
        log_name="daily-recommendation-generate",
        cli_name="generate-daily-recommendations",
        cli_help="执行一次每日推荐快照生成",
        cron_setting="daily_recommendation_generate_cron",
        handler=lambda reporter, _params: DailyRecommendationService.generate_latest_snapshot(
            progress_callback=reporter.progress_callback,
        ),
    ),
    JobDefinition(
        task_key="gfriends_filetree_refresh",
        log_name="gfriends-filetree-refresh",
        cli_name="refresh-gfriends-filetree",
        cli_help="拉取一次 GFriends Filetree 并写入本地缓存",
        cron_setting="gfriends_filetree_refresh_cron",
        handler=_run_gfriends_filetree_refresh,
    ),
    JobDefinition(
        task_key="activity_record_cleanup",
        log_name="activity-record-cleanup",
        cli_name="cleanup-activity-records",
        cli_help="执行一次活动中心记录清理（任务运行 / 已读通知）",
        cron_setting="activity_cleanup_cron",
        handler=lambda _reporter, _params: ActivityCleanupService().cleanup(),
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

    # 队列专属 key 冲突同样只隔离插件。
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
# 任务注册阶段也可能隔离插件；最终 provider registry 只能保留完整通过
# 所有宿主注册表的插件，避免被拒插件仍出现在媒体库 provider catalog。
_ACTIVE_PLUGINS = tuple(
    plugin
    for plugin in _ACTIVE_PLUGINS
    if PLUGIN_LOAD_ERRORS.get(plugin.plugin_id, {}).get("stage")
    != "registry_conflict"
)
refresh_media_provider_registry(_ACTIVE_PLUGINS)
JOB_REGISTRY_BY_KEY: dict[str, JobDefinition] = {job.task_key: job for job in JOB_REGISTRY}
