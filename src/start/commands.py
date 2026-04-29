#!/usr/bin/env python
# -*- encoding: utf-8 -*-
# here put the import lib

import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path

import click
from loguru import logger
from tqdm import tqdm

import src.common.logging as app_logging
from src.common.logging import configure_logging
from src.api.exception.errors import ApiError
from src.config.config import settings
from src.metadata.factory import build_dmm_provider, build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import BackgroundTaskRun, ResourceTaskState, init_database
from src.scheduler.progress import TqdmProgressAdapter
from src.scheduler.registry import JOB_REGISTRY
from src.schema.playback.media_libraries import MediaLibraryCreateRequest
from src.service.catalog import MovieThinCoverBackfillService
from src.service.catalog.movie_desc_translation_client import (
    MovieDescTranslationClient,
    MovieDescTranslationClientError,
)
from src.service.catalog.movie_desc_translation_test_support import (
    DEFAULT_TEST_TRANSLATION_PROMPT,
)
from src.service.playback import MediaFileScanService, MediaLibraryService
from src.service.system import TaskRunConflictError
from src.service.transfers import MediaImportService
from src.start.initdb import create_tables


@contextmanager
def _suppress_logs_for_json_output(enabled: bool):
    if not enabled:
        yield
        return

    previous_disable_level = logging.root.manager.disable
    root_logger = logging.getLogger()
    removed_sink_ids: list[int] = []
    for sink_id in (app_logging._LOGURU_STDERR_SINK_ID, 0):
        if sink_id is None or sink_id in removed_sink_ids:
            continue
        try:
            logger.remove(sink_id)
        except ValueError:
            continue
        removed_sink_ids.append(sink_id)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    root_logger.addHandler(logging.StreamHandler(sys.__stderr__))

    # 结构化输出模式下临时关闭日志，保证 stdout 只包含 JSON 载荷。
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous_disable_level)
        if removed_sink_ids:
            app_logging._LOGURU_STDERR_SINK_ID = None
            app_logging._DEFAULT_LOGURU_SINK_REMOVED = True


@click.group()
def main():
    """ """
    configure_logging()


def _ensure_database_ready():
    # 命令行入口只确保当前 schema 的表存在，不再承担旧库迁移职责。
    # 直接复用建表返回的目标数据库，避免命令链路再回退到残留的全局 proxy。
    database = create_tables()
    if database.is_closed():
        database.connect()
    logger.info("Database ready for command execution")
    return database


def _connect_database_for_migration():
    # 迁移入口必须先连接旧库，不能提前按当前模型创建索引，否则旧表缺列会导致建表阶段失败。
    database = init_database(settings.database)
    if database.is_closed():
        database.connect()
    logger.info("Database connected for migration")
    return database


def _merge_migration_summaries(*summaries):
    from src.start.migrations import MigrationExecution, MigrationRunSummary

    merged: dict[str, MigrationExecution] = {}
    for summary in summaries:
        for execution in summary.executed:
            previous = merged.get(execution.name)
            if previous is None or (execution.applied and not previous.applied):
                merged[execution.name] = execution
    return MigrationRunSummary(executed=list(merged.values()))


def _echo_json(payload: dict) -> None:
    click.echo(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _load_required_text_input(
    *,
    direct_value: str | None,
    file_value: str | None,
    direct_option_name: str,
    file_option_name: str,
) -> str:
    # 文本输入要求二选一，避免命令在“直接传参”和“文件读入”之间出现歧义。
    has_direct_value = bool((direct_value or "").strip())
    has_file_value = bool((file_value or "").strip())
    if has_direct_value == has_file_value:
        raise click.ClickException(f"must provide exactly one of {direct_option_name} or {file_option_name}")
    if has_direct_value:
        return str(direct_value).strip()
    return Path(str(file_value)).read_text(encoding="utf-8").strip()


def _load_optional_text_input(
    *,
    direct_value: str | None,
    file_value: str | None,
    default_value: str,
    direct_option_name: str,
    file_option_name: str,
) -> str:
    has_direct_value = bool((direct_value or "").strip())
    has_file_value = bool((file_value or "").strip())
    if has_direct_value and has_file_value:
        raise click.ClickException(f"cannot provide both {direct_option_name} and {file_option_name}")
    if has_direct_value:
        return str(direct_value).strip()
    if has_file_value:
        return Path(str(file_value)).read_text(encoding="utf-8").strip()
    return default_value


def _fail_command(*, output_json: bool, message: str, error: dict | None = None) -> None:
    normalized_message = str(message).strip()
    if output_json:
        payload = {
            "ok": False,
            "message": normalized_message,
        }
        if error is not None:
            payload["error"] = error
        _echo_json(payload)
        raise click.exceptions.Exit(1)
    raise click.ClickException(normalized_message)


def _fail_for_translation_error(*, exc: MovieDescTranslationClientError, output_json: bool) -> None:
    _fail_command(
        output_json=output_json,
        message=exc.message,
        error={
            "type": "translation_client_error",
            "status_code": exc.status_code,
            "error_code": exc.error_code,
            "message": exc.message,
        },
    )


def _fail_for_metadata_error(*, exc: Exception, output_json: bool) -> None:
    if isinstance(exc, MetadataNotFoundError):
        _fail_command(
            output_json=output_json,
            message=str(exc),
            error={
                "type": "metadata_not_found",
                "resource": exc.resource,
                "lookup_value": exc.lookup_value,
                "message": str(exc),
            },
        )
    if isinstance(exc, MetadataRequestError):
        _fail_command(
            output_json=output_json,
            message=str(exc),
            error={
                "type": "metadata_request_error",
                "method": exc.method,
                "url": exc.url,
                "message": str(exc),
            },
        )
    raise exc


def _emit_command_success(
    *,
    output_json: bool,
    payload: dict,
    summary_title: str,
    inline_fields: list[tuple[str, object]] | None = None,
    multiline_fields: list[tuple[str, object]] | None = None,
) -> None:
    if output_json:
        _echo_json(payload)
        return

    header = summary_title
    normalized_inline_fields = inline_fields or []
    if normalized_inline_fields:
        inline_text = " ".join(f"{key}={value}" for key, value in normalized_inline_fields)
        header = f"{summary_title} {inline_text}"

    text_lines = [header]
    for key, value in multiline_fields or []:
        text_lines.append(f"{key}={value}")
    click.echo("\n".join(text_lines))


@main.command()
def initdb():
    """
    初始化数据库
    """
    from src.start.initdb import initdb

    initdb()


@main.command()
def migrate():
    """执行待应用的数据库迁移"""
    logger.info("CLI migrate start")
    from src.start.migrations import run_pending_migrations

    # 旧库必须先执行字段迁移，再按当前模型补齐新增表和索引。
    database = _connect_database_for_migration()
    before_create_summary = run_pending_migrations(database)
    database = _ensure_database_ready()
    after_create_summary = run_pending_migrations(database)
    summary = _merge_migration_summaries(before_create_summary, after_create_summary)
    for execution in summary.executed:
        status_text = "applied" if execution.applied else "skipped"
        click.echo(f"{status_text}: {execution.name}")
    click.echo(
        "migrate finished: "
        f"applied={summary.applied_count} "
        f"skipped={summary.skipped_count} "
        f"total={len(summary.executed)}"
    )


@main.command(name="test-trans")
@click.option("--text", type=str, help="Text to translate.")
@click.option(
    "--text-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=str),
    help="Read source text from file.",
)
@click.option("--prompt", type=str, help="Custom translation prompt.")
@click.option(
    "--prompt-file",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=str),
    help="Read custom prompt from file.",
)
@click.option("--base-url", type=str, help="Override translation base URL.")
@click.option("--api-key", type=str, help="Override translation API key.")
@click.option("--model", type=str, help="Override translation model.")
@click.option("--json", "output_json", is_flag=True, help="Print structured JSON output.")
def test_translation(
    text: str | None,
    text_file: str | None,
    prompt: str | None,
    prompt_file: str | None,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    output_json: bool,
):
    with _suppress_logs_for_json_output(output_json):
        if not output_json:
            logger.info(
                "CLI test-trans start base_url={} model={}",
                base_url or settings.movie_info_translation.base_url,
                model or settings.movie_info_translation.model,
            )
        try:
            source_text = _load_required_text_input(
                direct_value=text,
                file_value=text_file,
                direct_option_name="--text",
                file_option_name="--text-file",
            )
            system_prompt = _load_optional_text_input(
                direct_value=prompt,
                file_value=prompt_file,
                default_value=DEFAULT_TEST_TRANSLATION_PROMPT,
                direct_option_name="--prompt",
                file_option_name="--prompt-file",
            )
        except click.ClickException as exc:
            _fail_command(output_json=output_json, message=exc.message)
            return

        client = MovieDescTranslationClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
        )
        try:
            translated_text = client.translate(system_prompt=system_prompt, source_text=source_text)
        except MovieDescTranslationClientError as exc:
            _fail_for_translation_error(exc=exc, output_json=output_json)
            return

        payload = {
            "ok": True,
            "service": "translation",
            "base_url": client.base_url,
            "model": client.model,
            "source_text": source_text,
            "system_prompt": system_prompt,
            "translated_text": translated_text,
        }
        _emit_command_success(
            output_json=output_json,
            payload=payload,
            summary_title="translation test succeeded:",
            inline_fields=[
                ("base_url", client.base_url),
                ("model", client.model),
            ],
            multiline_fields=[
                ("source_text", source_text),
                ("translated_text", translated_text),
            ],
        )


@main.command(name="test-javdb")
@click.option("--movie-number", required=True, type=str, help="Movie number to query from JavDB.")
@click.option(
    "--use-metadata-proxy/--no-use-metadata-proxy",
    default=False,
    show_default=True,
    help="Whether to route JavDB via metadata proxy settings.",
)
@click.option("--json", "output_json", is_flag=True, help="Print structured JSON output.")
def test_javdb(movie_number: str, use_metadata_proxy: bool, output_json: bool):
    with _suppress_logs_for_json_output(output_json):
        if not output_json:
            logger.info("CLI test-javdb start movie_number={} use_metadata_proxy={}", movie_number, use_metadata_proxy)
        provider = build_javdb_provider(use_metadata_proxy=use_metadata_proxy)
        try:
            detail = provider.get_movie_by_number(movie_number)
        except (MetadataNotFoundError, MetadataRequestError) as exc:
            _fail_for_metadata_error(exc=exc, output_json=output_json)
            return

        summary_excerpt = (detail.summary or "").strip()
        payload = {
            "ok": True,
            "service": "javdb",
            "movie_number": detail.movie_number,
            "javdb_id": detail.javdb_id,
            "title": detail.title,
            "actors_count": len(detail.actors),
            "tags_count": len(detail.tags),
            "summary": summary_excerpt,
            "release_date": detail.release_date,
            "use_metadata_proxy": use_metadata_proxy,
        }
        _emit_command_success(
            output_json=output_json,
            payload=payload,
            summary_title="javdb test succeeded:",
            inline_fields=[
                ("movie_number", detail.movie_number),
                ("javdb_id", detail.javdb_id),
                ("title", detail.title),
                ("actors", len(detail.actors)),
                ("tags", len(detail.tags)),
            ],
            multiline_fields=[("summary", summary_excerpt)],
        )


@main.command(name="test-dmm")
@click.option("--movie-number", required=True, type=str, help="Movie number to query from DMM.")
@click.option("--json", "output_json", is_flag=True, help="Print structured JSON output.")
def test_dmm(movie_number: str, output_json: bool):
    with _suppress_logs_for_json_output(output_json):
        if not output_json:
            logger.info("CLI test-dmm start movie_number={}", movie_number)
        provider = build_dmm_provider()
        try:
            description = provider.get_movie_desc(movie_number)
        except (MetadataNotFoundError, MetadataRequestError) as exc:
            _fail_for_metadata_error(exc=exc, output_json=output_json)
            return

        payload = {
            "ok": True,
            "service": "dmm",
            "movie_number": movie_number,
            "description": description,
        }
        _emit_command_success(
            output_json=output_json,
            payload=payload,
            summary_title="dmm test succeeded:",
            inline_fields=[("movie_number", movie_number)],
            multiline_fields=[("description", description)],
        )


@main.group(invoke_without_command=True)
@click.pass_context
def aps(ctx: click.Context):
    """定时任务相关命令"""
    if ctx.invoked_subcommand is not None:
        # 单次 APS 子命令不会走守护进程 aps() 启动流程，这里补齐数据库准备。
        _ensure_database_ready()
        return

    from src.start.aps import aps as start_aps

    start_aps()


# ---------------------------------------------------------------------------
# 基于 JOB_REGISTRY 自动注册 APS 子命令，每个命令带 tqdm 进度条
# ---------------------------------------------------------------------------


def _register_aps_command(job_def):
    @aps.command(name=job_def.cli_name, help=job_def.cli_help)
    def _cmd():
        from src.start.aps import run_job

        adapter = TqdmProgressAdapter()
        try:
            stats = run_job(job_def, trigger_type="manual", extra_callbacks=[adapter.callback])
        except TaskRunConflictError as exc:
            raise click.ClickException(str(exc))
        finally:
            adapter.close()
        if job_def.format_stats and isinstance(stats, dict):
            click.echo(job_def.format_stats(stats))
        else:
            click.echo(f"{job_def.cli_name} finished: {stats}")

    return _cmd


for _job_def in JOB_REGISTRY:
    _register_aps_command(_job_def)


# ---------------------------------------------------------------------------
# 非 APS 命令（保持不变）
# ---------------------------------------------------------------------------


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


@main.command(name="backfill-movie-thin-cover-images")
def backfill_movie_thin_cover_images():
    logger.info("CLI backfill-movie-thin-cover-images start")
    _ensure_database_ready()
    service = MovieThinCoverBackfillService()
    stats = service.backfill_missing_thin_cover_images()
    logger.info(
        "CLI backfill-movie-thin-cover-images finished scanned_movies={} updated_movies={} skipped_movies={} failed_movies={}",
        stats["scanned_movies"],
        stats["updated_movies"],
        stats["skipped_movies"],
        stats["failed_movies"],
    )
    click.echo(
        "movie thin cover image backfill finished: "
        f"scanned_movies={stats['scanned_movies']} "
        f"updated_movies={stats['updated_movies']} "
        f"skipped_movies={stats['skipped_movies']} "
        f"failed_movies={stats['failed_movies']}"
    )


@main.command(name="cleanup-movie-subtitle-fetch-history")
def cleanup_movie_subtitle_fetch_history():
    logger.info("CLI cleanup-movie-subtitle-fetch-history start")
    _ensure_database_ready()
    # 只清理废弃任务留下的运行痕迹，不删除任何字幕文件或 Subtitle 记录。
    deleted_task_runs = BackgroundTaskRun.delete().where(BackgroundTaskRun.task_key == "movie_subtitle_fetch").execute()
    deleted_resource_task_states = (
        ResourceTaskState.delete()
        .where(ResourceTaskState.task_key == "movie_subtitle_fetch")
        .execute()
    )
    logger.info(
        "CLI cleanup-movie-subtitle-fetch-history finished deleted_task_runs={} deleted_resource_task_states={}",
        deleted_task_runs,
        deleted_resource_task_states,
    )
    click.echo(
        "movie subtitle fetch history cleanup finished: "
        f"deleted_task_runs={deleted_task_runs} "
        f"deleted_resource_task_states={deleted_resource_task_states}"
    )


@main.command(name="scan-media-files")
def scan_media_files():
    logger.info("CLI scan-media-files start")
    _ensure_database_ready()
    service = MediaFileScanService()
    stats = service.scan_media_files()
    logger.info(
        "CLI scan-media-files finished scanned_media={} updated_media={} skipped_media={} failed_media={} invalidated_media={} revived_media={}",
        stats["scanned_media"],
        stats["updated_media"],
        stats["skipped_media"],
        stats["failed_media"],
        stats["invalidated_media"],
        stats["revived_media"],
    )
    click.echo(
        "media file scan finished: "
        f"scanned_media={stats['scanned_media']} "
        f"updated_media={stats['updated_media']} "
        f"skipped_media={stats['skipped_media']} "
        f"failed_media={stats['failed_media']} "
        f"invalidated_media={stats['invalidated_media']} "
        f"revived_media={stats['revived_media']}"
    )


if __name__ == "__main__":
    main()
