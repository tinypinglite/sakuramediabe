from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel, ConfigDict

from src.service.catalog import (
    MovieCollectionService,
    MovieDescTranslationService,
    MovieDescSyncService,
    MovieHeatService,
    MovieInteractionSyncService,
    MovieTitleTranslationService,
    SubscribedActorMovieSyncService,
)
from src.service.discovery import (
    HotReviewSyncService,
    ImageSearchIndexService,
    MovieRecommendationService,
    RankingSyncService,
)
from src.service.playback import MediaFileScanService, MediaThumbnailService
from src.service.transfers import DownloadSyncService, SubscribedMovieAutoDownloadService


class JobDefinition(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    task_key: str
    log_name: str
    cli_name: str
    cli_help: str
    cron_setting: str
    service_factory: Callable[..., Any]
    # 业务回收仅处理每条记录的 running 状态，不再承担 task_run / mutex 回收。
    business_recovery: Callable[[], dict[str, int]] | None = None
    format_stats: Callable[[dict[str, Any]], str] | None = None


def _resolve_stat_value(
    stats: dict[str, Any],
    source_keys: str | tuple[str, ...],
    default: Any,
) -> Any:
    if isinstance(source_keys, tuple):
        for source_key in source_keys:
            if source_key in stats:
                return stats[source_key]
        return default
    return stats.get(source_keys, default)


def _build_stats_formatter(
    prefix: str,
    *fields: tuple[str, str | tuple[str, ...], Any],
) -> Callable[[dict[str, Any]], str]:
    def _formatter(stats: dict[str, Any]) -> str:
        # 统一在这里处理缺省值和别名字段，避免注册表里散落大量重复的 s.get(...)。
        formatted_fields = [
            f"{field_name}={_resolve_stat_value(stats, source_keys, default)}"
            for field_name, source_keys, default in fields
        ]
        return f"{prefix} {' '.join(formatted_fields)}"

    return _formatter


# ---------------------------------------------------------------------------
# 任务注册表
# ---------------------------------------------------------------------------

JOB_REGISTRY: list[JobDefinition] = [
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
            ("total_actors", "total_actors", 0),
            ("success_actors", "success_actors", 0),
            ("failed_actors", "failed_actors", 0),
            ("imported_movies", "imported_movies", 0),
        ),
    ),
    JobDefinition(
        task_key="subscribed_movie_auto_download",
        log_name="subscribed-movie-auto-download",
        cli_name="auto-download-subscribed-movies",
        cli_help="执行一次已订阅缺失影片自动下载",
        cron_setting="subscribed_movie_auto_download_cron",
        service_factory=lambda _reporter: SubscribedMovieAutoDownloadService().run(),
        format_stats=_build_stats_formatter(
            "auto download finished:",
            ("candidate_movies", "candidate_movies", 0),
            ("searched_movies", "searched_movies", 0),
            ("submitted_movies", "submitted_movies", 0),
            ("no_candidate_movies", "no_candidate_movies", 0),
            ("skipped_movies", "skipped_movies", 0),
            ("failed_movies", "failed_movies", 0),
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
            ("candidate_count", "candidate_count", 0),
            ("updated_count", "updated_count", 0),
            ("formula_version", "formula_version", "unknown"),
        ),
    ),
    JobDefinition(
        task_key="movie_interaction_sync",
        log_name="movie-interaction-sync",
        cli_name="sync-movie-interactions",
        cli_help="执行一次影片互动数同步",
        cron_setting="movie_interaction_sync_cron",
        service_factory=lambda reporter: MovieInteractionSyncService().run(
            progress_callback=reporter.progress_callback,
        ),
        business_recovery=lambda: {
            "recovered_running_movies": MovieInteractionSyncService.recover_interrupted_running_movies(
                error_message=MovieInteractionSyncService.INTERRUPTED_SYNC_ERROR_MESSAGE,
            )
        },
        format_stats=_build_stats_formatter(
            "movie interaction sync finished:",
            ("candidate_movies", "candidate_movies", 0),
            ("processed_movies", "processed_movies", 0),
            ("succeeded_movies", "succeeded_movies", 0),
            ("failed_movies", "failed_movies", 0),
            ("updated_movies", "updated_movies", 0),
            ("unchanged_movies", "unchanged_movies", 0),
            ("heat_updated_movies", "heat_updated_movies", 0),
        ),
    ),
    JobDefinition(
        task_key="ranking_sync",
        log_name="ranking-sync",
        cli_name="sync-rankings",
        cli_help="执行一次排行榜同步",
        cron_setting="ranking_sync_cron",
        service_factory=lambda reporter: RankingSyncService().sync_all_rankings(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "ranking sync finished:",
            ("total_targets", "total_targets", 0),
            ("success_targets", "success_targets", 0),
            ("failed_targets", "failed_targets", 0),
            ("fetched_numbers", "fetched_numbers", 0),
            ("imported_movies", "imported_movies", 0),
            ("skipped_movies", "skipped_movies", 0),
            ("stored_items", "stored_items", 0),
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
            ("total_periods", "total_periods", 0),
            ("success_periods", "success_periods", 0),
            ("failed_periods", "failed_periods", 0),
            ("fetched_reviews", "fetched_reviews", 0),
            ("imported_movies", "imported_movies", 0),
            ("skipped_reviews", "skipped_reviews", 0),
            ("stored_items", "stored_items", 0),
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
            ("total_movies", "total_movies", 0),
            ("matched_count", "matched_count", 0),
            ("updated_to_collection_count", "updated_to_collection_count", 0),
            ("updated_to_single_count", "updated_to_single_count", 0),
            ("unchanged_count", "unchanged_count", 0),
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
            ("scanned_media", "scanned_media", 0),
            ("updated_media", "updated_media", 0),
            ("skipped_media", "skipped_media", 0),
            ("failed_media", "failed_media", 0),
            ("invalidated_media", "invalidated_media", 0),
            ("revived_media", "revived_media", 0),
        ),
    ),
    JobDefinition(
        task_key="movie_desc_sync",
        log_name="movie-desc-sync",
        cli_name="sync-movie-desc",
        cli_help="执行一次影片描述回填",
        cron_setting="movie_desc_sync_cron",
        service_factory=lambda reporter: MovieDescSyncService().run(
            progress_callback=reporter.progress_callback,
        ),
        business_recovery=lambda: {
            "recovered_running_movies": MovieDescSyncService.recover_interrupted_running_movies(
                error_message=MovieDescSyncService.INTERRUPTED_FETCH_ERROR_MESSAGE,
            )
        },
        format_stats=_build_stats_formatter(
            "movie desc sync finished:",
            ("candidate_movies", "candidate_movies", 0),
            ("processed_movies", "processed_movies", 0),
            ("succeeded_movies", "succeeded_movies", 0),
            ("failed_movies", "failed_movies", 0),
            ("updated_movies", "updated_movies", 0),
            ("skipped_movies", "skipped_movies", 0),
        ),
    ),
    JobDefinition(
        task_key="movie_desc_translation",
        log_name="movie-desc-translation",
        cli_name="translate-movie-desc",
        cli_help="执行一次影片简介翻译",
        cron_setting="movie_desc_translation_cron",
        service_factory=lambda reporter: MovieDescTranslationService().run(
            progress_callback=reporter.progress_callback,
        ),
        business_recovery=lambda: {
            "recovered_running_movies": MovieDescTranslationService.recover_interrupted_running_movies(
                error_message=MovieDescTranslationService.INTERRUPTED_TRANSLATION_ERROR_MESSAGE,
            )
        },
        format_stats=_build_stats_formatter(
            "movie desc translation finished:",
            ("candidate_movies", "candidate_movies", 0),
            ("processed_movies", "processed_movies", 0),
            ("succeeded_movies", "succeeded_movies", 0),
            ("failed_movies", "failed_movies", 0),
            ("updated_movies", "updated_movies", 0),
            ("skipped_movies", "skipped_movies", 0),
        ),
    ),
    JobDefinition(
        task_key="movie_title_translation",
        log_name="movie-title-translation",
        cli_name="translate-movie-title",
        cli_help="执行一次影片标题翻译",
        cron_setting="movie_title_translation_cron",
        service_factory=lambda reporter: MovieTitleTranslationService().run(
            progress_callback=reporter.progress_callback,
        ),
        business_recovery=lambda: {
            "recovered_running_movies": MovieTitleTranslationService.recover_interrupted_running_movies(
                error_message=MovieTitleTranslationService.INTERRUPTED_TRANSLATION_ERROR_MESSAGE,
            )
        },
        format_stats=_build_stats_formatter(
            "movie title translation finished:",
            ("candidate_movies", "candidate_movies", 0),
            ("processed_movies", "processed_movies", 0),
            ("succeeded_movies", "succeeded_movies", 0),
            ("failed_movies", "failed_movies", 0),
            ("updated_movies", "updated_movies", 0),
            ("skipped_movies", "skipped_movies", 0),
        ),
    ),
    JobDefinition(
        task_key="media_thumbnail_generation",
        log_name="media-thumbnail-generation",
        cli_name="generate-media-thumbnails",
        cli_help="执行一次媒体缩略图生成",
        cron_setting="media_thumbnail_cron",
        service_factory=lambda reporter: MediaThumbnailService.generate_pending_thumbnails(
            progress_callback=reporter.progress_callback,
        ),
        format_stats=_build_stats_formatter(
            "thumbnail generation finished:",
            ("pending_media", "pending_media", 0),
            ("successful_media", "successful_media", 0),
            ("generated_thumbnails", "generated_thumbnails", 0),
            ("retryable_failed_media", "retryable_failed_media", 0),
            ("terminal_failed_media", "terminal_failed_media", 0),
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
            ("pending_thumbnails", "pending_thumbnails", 0),
            ("successful_thumbnails", "successful_thumbnails", 0),
            ("failed_thumbnails", "failed_thumbnails", 0),
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
            ("total_movies", "total_movies", 0),
            ("processed_movies", "processed_movies", 0),
            ("stored_pairs", "stored_pairs", 0),
            ("skipped_movies", "skipped_movies", 0),
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
            ("compacted", "compacted", False),
        ),
    ),
]

JOB_REGISTRY_BY_KEY: dict[str, JobDefinition] = {j.task_key: j for j in JOB_REGISTRY}
