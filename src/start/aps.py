from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from src.config.config import settings
from src.model import get_database, init_database
from src.scheduler import run_logged_task
from src.service.catalog import MovieCollectionService, MovieHeatService, SubscribedActorMovieSyncService
from src.service.discovery import ImageSearchIndexService
from src.service.playback import MediaThumbnailService
from src.service.transfers import DownloadSyncService


def _ensure_database_ready():
    try:
        database = get_database()
        logger.debug("Scheduler database proxy already initialized")
    except RuntimeError:
        logger.info("Scheduler database proxy not initialized, initializing from settings")
        database = init_database(settings.database)
    if database.is_closed():
        logger.info("Scheduler database is closed, connecting now")
        database.connect()
    return database


def run_subscribed_actor_movie_sync_job():
    _ensure_database_ready()
    return run_logged_task(
        "actor-subscription-sync",
        lambda: SubscribedActorMovieSyncService().sync_subscribed_actor_movies(),
    )


def run_movie_heat_update_job():
    _ensure_database_ready()
    return run_logged_task(
        "movie-heat-update",
        lambda: MovieHeatService.update_movie_heat(),
    )


def run_movie_collection_sync_job():
    _ensure_database_ready()
    return run_logged_task(
        "movie-collection-sync",
        lambda: MovieCollectionService.sync_movie_collections(),
    )


def run_download_task_sync_job():
    _ensure_database_ready()
    return run_logged_task(
        "download-task-sync",
        lambda: DownloadSyncService().sync_all_clients(),
    )


def run_download_task_auto_import_job():
    _ensure_database_ready()
    return run_logged_task(
        "download-task-auto-import",
        lambda: DownloadSyncService().enqueue_auto_imports(),
    )


def run_media_thumbnail_generation_job():
    _ensure_database_ready()
    return run_logged_task(
        "media-thumbnail-generation",
        lambda: MediaThumbnailService.generate_pending_thumbnails(),
    )


def run_image_search_index_job():
    _ensure_database_ready()
    return run_logged_task(
        "image-search-index",
        lambda: ImageSearchIndexService().index_pending_thumbnails(),
    )


def run_image_search_optimize_job():
    _ensure_database_ready()
    return run_logged_task(
        "image-search-optimize",
        lambda: ImageSearchIndexService().optimize_index(),
    )


def build_scheduler() -> BlockingScheduler:
    timezone = ZoneInfo(settings.scheduler.timezone)
    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(2)},
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone=timezone,
    )
    # 抓取已订阅女优的影片
    scheduler.add_job(
        run_subscribed_actor_movie_sync_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.actor_subscription_sync_cron,
            timezone=timezone,
        ),
        id="actor_subscription_sync",
        replace_existing=True,
    )

    # 计算热度值
    scheduler.add_job(
        run_movie_heat_update_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.movie_heat_cron,
            timezone=timezone,
        ),
        id="movie_heat_update",
        replace_existing=True,
    )

    # 根据本地规则同步合集影片标记
    scheduler.add_job(
        run_movie_collection_sync_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.movie_collection_sync_cron,
            timezone=timezone,
        ),
        id="movie_collection_sync",
        replace_existing=True,
    )

    # 同步下载任务状态
    scheduler.add_job(
        run_download_task_sync_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.download_task_sync_cron,
            timezone=timezone,
        ),
        id="download_task_sync",
        replace_existing=True,
    )

    # 导入已经完成的下载任务
    scheduler.add_job(
        run_download_task_auto_import_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.download_task_auto_import_cron,
            timezone=timezone,
        ),
        id="download_task_auto_import",
        replace_existing=True,
    )
    scheduler.add_job(
        run_media_thumbnail_generation_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.media_thumbnail_cron,
            timezone=timezone,
        ),
        id="media_thumbnail_generation",
        replace_existing=True,
    )
    scheduler.add_job(
        run_image_search_index_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.image_search_index_cron,
            timezone=timezone,
        ),
        id="image_search_index",
        replace_existing=True,
    )
    scheduler.add_job(
        run_image_search_optimize_job,
        trigger=CronTrigger.from_crontab(
            settings.scheduler.image_search_optimize_cron,
            timezone=timezone,
        ),
        id="image_search_optimize",
        replace_existing=True,
    )
    return scheduler


def aps():
    if not settings.scheduler.enabled:
        logger.info("Scheduler is disabled by configuration")
        return
    scheduler = build_scheduler()
    logger.info(
        "Starting scheduler timezone={} actor_subscription_sync_cron={} movie_collection_sync_cron={} movie_heat_cron={} download_task_sync_cron={} download_task_auto_import_cron={} media_thumbnail_cron={} image_search_index_cron={} image_search_optimize_cron={}",
        settings.scheduler.timezone,
        settings.scheduler.actor_subscription_sync_cron,
        settings.scheduler.movie_collection_sync_cron,
        settings.scheduler.movie_heat_cron,
        settings.scheduler.download_task_sync_cron,
        settings.scheduler.download_task_auto_import_cron,
        settings.scheduler.media_thumbnail_cron,
        settings.scheduler.image_search_index_cron,
        settings.scheduler.image_search_optimize_cron,
    )
    scheduler.start()
