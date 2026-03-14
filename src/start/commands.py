#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# here put the import lib

import click
from loguru import logger
from tqdm import tqdm

from src.common.logging import configure_logging
from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import get_database, init_database
from src.schema.playback.media_libraries import MediaLibraryCreateRequest
from src.service.playback import MediaLibraryService
from src.service.transfers import MediaImportService


@click.group()
def main():
    """ """
    configure_logging()
    logger.info("----command----")


def _ensure_database_ready():
    try:
        database = get_database()
        logger.debug("Database proxy already initialized")
    except RuntimeError:
        logger.info("Database proxy not initialized, initializing from settings")
        database = init_database(settings.database)
    if database.is_closed():
        logger.info("Database is closed, connecting now")
        database.connect()
    logger.info("Database ready for command execution")
    return database


@main.command()
def initdb():
    """
    初始化数据库
    """
    from src.start.initdb import initdb

    initdb()


@main.group(invoke_without_command=True)
@click.pass_context
def aps(ctx: click.Context):
    """定时任务相关命令"""
    if ctx.invoked_subcommand is not None:
        return

    from src.start.aps import aps as start_aps

    start_aps()


@aps.command(name="sync-subscribed-actor-movies")
def aps_sync_subscribed_actor_movies():
    """执行一次订阅女优影片抓取"""
    from src.start.aps import run_subscribed_actor_movie_sync_job

    stats = run_subscribed_actor_movie_sync_job()
    click.echo(
        "sync finished: "
        f"total_actors={stats['total_actors']} "
        f"success_actors={stats['success_actors']} "
        f"failed_actors={stats['failed_actors']} "
        f"imported_movies={stats['imported_movies']}"
    )


@aps.command(name="update-movie-heat")
def aps_update_movie_heat():
    """执行一次影片热度重算"""
    from src.start.aps import run_movie_heat_update_job

    stats = run_movie_heat_update_job()
    click.echo(
        "heat update finished: "
        f"candidate_count={stats['candidate_count']} "
        f"updated_count={stats['updated_count']} "
        f"formula_version={stats['formula_version']}"
    )


@aps.command(name="auto-download-subscribed-movies")
def aps_auto_download_subscribed_movies():
    """执行一次已订阅缺失影片自动下载"""
    from src.start.aps import run_subscribed_movie_auto_download_job

    stats = run_subscribed_movie_auto_download_job()
    click.echo(
        "auto download finished: "
        f"candidate_movies={stats['candidate_movies']} "
        f"searched_movies={stats['searched_movies']} "
        f"submitted_movies={stats['submitted_movies']} "
        f"no_candidate_movies={stats['no_candidate_movies']} "
        f"skipped_movies={stats['skipped_movies']} "
        f"failed_movies={stats['failed_movies']}"
    )


@aps.command(name="sync-movie-collections")
def aps_sync_movie_collections():
    """执行一次合集影片标记同步"""
    from src.start.aps import run_movie_collection_sync_job

    stats = run_movie_collection_sync_job()
    click.echo(
        "collection sync finished: "
        f"total_movies={stats['total_movies']} "
        f"matched_count={stats['matched_count']} "
        f"updated_to_collection_count={stats['updated_to_collection_count']} "
        f"updated_to_single_count={stats['updated_to_single_count']} "
        f"unchanged_count={stats['unchanged_count']}"
    )


@aps.command(name="generate-media-thumbnails")
def aps_generate_media_thumbnails():
    """执行一次媒体缩略图生成"""
    from src.start.aps import run_media_thumbnail_generation_job

    stats = run_media_thumbnail_generation_job()
    click.echo(
        "thumbnail generation finished: "
        f"pending_media={stats['pending_media']} "
        f"successful_media={stats['successful_media']} "
        f"generated_thumbnails={stats['generated_thumbnails']} "
        f"retryable_failed_media={stats['retryable_failed_media']} "
        f"terminal_failed_media={stats['terminal_failed_media']}"
    )


@aps.command(name="index-image-search-thumbnails")
def aps_index_image_search_thumbnails():
    """执行一次以图搜图缩略图向量索引"""
    from src.start.aps import run_image_search_index_job

    stats = run_image_search_index_job()
    click.echo(
        "image search index finished: "
        f"pending_thumbnails={stats['pending_thumbnails']} "
        f"successful_thumbnails={stats['successful_thumbnails']} "
        f"failed_thumbnails={stats['failed_thumbnails']}"
    )


@aps.command(name="optimize-image-search-index")
def aps_optimize_image_search_index():
    """执行一次以图搜图向量索引优化"""
    from src.start.aps import run_image_search_optimize_job

    stats = run_image_search_optimize_job()
    click.echo(
        "image search optimize finished: "
        f"compacted={stats.get('compacted', False)}"
    )


@main.command(name="import-media")
@click.option(
    "--source-path",
    required=True,
    type=click.Path(exists=True, dir_okay=True, file_okay=False),
    help="Import source directory.",
)
@click.option("--library-id", required=True, type=int, help="Target media library id.")
def import_media(source_path: str, library_id: int):
    logger.info("CLI import-media start source_path={} library_id={}", source_path, library_id)
    _ensure_database_ready()
    service = MediaImportService()
    progress_bar = None

    def _handle_progress(event: dict[str, object]) -> None:
        nonlocal progress_bar
        event_type = str(event.get("event", ""))
        if event_type == "scan_complete":
            progress_bar = tqdm(
                total=int(event.get("total_movies", 0)),
                unit="movie",
                dynamic_ncols=True,
            )
            return
        if progress_bar is None:
            return

        stage = event.get("stage")
        if stage:
            progress_bar.set_description(str(stage))

        postfix = {
            "imported": int(event.get("imported_count", 0)),
            "skipped": int(event.get("skipped_count", 0)),
            "failed": int(event.get("failed_count", 0)),
        }
        movie_number = event.get("movie_number")
        if movie_number:
            postfix["movie_number"] = str(movie_number)
        progress_bar.set_postfix(postfix)

        if event_type == "movie_finished":
            progress_bar.update(1)

    click.echo("scanning source...")
    try:
        job = service.import_from_source(
            source_path=source_path,
            library_id=library_id,
            progress_callback=_handle_progress,
        )
    except ValueError as exc:
        logger.warning("CLI import-media validation failed detail={}", exc)
        raise click.ClickException(str(exc))
    except Exception:
        logger.exception("CLI import-media crashed source_path={} library_id={}", source_path, library_id)
        raise
    finally:
        if progress_bar is not None:
            progress_bar.close()

    logger.info(
        "CLI import-media finished job_id={} state={} imported={} skipped={} failed={}",
        job.id,
        job.state,
        job.imported_count,
        job.skipped_count,
        job.failed_count,
    )
    click.echo(
        "import finished: "
        f"job_id={job.id} "
        f"state={job.state} "
        f"imported={job.imported_count} "
        f"skipped={job.skipped_count} "
        f"failed={job.failed_count}"
    )


@main.command(name="add-media-library")
@click.option("--name", required=True, type=str, help="Media library name.")
@click.option(
    "--root-path",
    required=True,
    type=str,
    help="Absolute root path for media library.",
)
def add_media_library(name: str, root_path: str):
    logger.info("CLI add-media-library start name={} root_path={}", name, root_path)
    _ensure_database_ready()
    try:
        library = MediaLibraryService.create_library(
            MediaLibraryCreateRequest(name=name, root_path=root_path)
        )
    except ApiError as exc:
        logger.warning(
            "CLI add-media-library validation failed code={} detail={}",
            exc.code,
            exc.details,
        )
        raise click.ClickException(exc.code)
    except Exception:
        logger.exception("CLI add-media-library crashed name={} root_path={}", name, root_path)
        raise

    logger.info(
        "CLI add-media-library finished library_id={} name={} root_path={}",
        library.id,
        library.name,
        library.root_path,
    )
    click.echo(
        "media library created: "
        f"library_id={library.id} "
        f"name={library.name} "
        f"root_path={library.root_path}"
    )


if __name__ == "__main__":
    main()
