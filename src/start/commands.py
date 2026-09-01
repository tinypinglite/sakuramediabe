import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
from loguru import logger

import src.common.logging as app_logging
from src.common.logging import configure_logging
from src.config.config import (
    DEFAULT_SIGLIP2_INFERENCE_URL,
    LEGACY_JOYTAG_INFERENCE_URL,
    settings,
)
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import init_database
from src.plugins.manager import PluginManager
from src.service.catalog import MovieThinCoverBackfillService
from src.service.system import TaskRunConflictError
from src.service.system.plugin_removal_service import PluginRemovalService
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
    from src.config.config import ensure_runtime_config
    from src.start.initdb import initdb

    # 容器首启在 API 启动前先把全量默认配置与生成密钥落盘到目标 config.toml，后续进程读到稳定值。
    ensure_runtime_config()
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


@main.command(name="upgrade-v053")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Build and validate the complete upgrade plan without database writes.",
)
def upgrade_v053(dry_run: bool):
    """为新库预装官方存储插件，并单向迁移精确 v0.5.3 数据。"""
    from src.plugins.bundled_providers import install_bundled_provider_plugins_once
    from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins
    from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
    from src.start.legacy_v053_upgrade import (
        LegacyV053UpgradeError,
        classify_database_schema,
        cleanup_legacy_v053_qdrant_collections,
        upgrade_v053_database,
    )

    logger.info("v0.5.3 upgrade command started")
    database = _connect_database_for_migration()
    state = classify_database_schema(database)
    logger.info("v0.5.3 upgrade command schema state={}", state)
    if state == "unsupported":
        raise click.ClickException(
            "unsupported_schema: only the exact v0.5.3 schema can use this bridge"
        )
    if (
        state == "legacy_v053"
        and settings.image_search.inference_base_url
        not in {LEGACY_JOYTAG_INFERENCE_URL, DEFAULT_SIGLIP2_INFERENCE_URL}
    ):
        logger.warning(
            "v0.5.3 upgrade preserved a custom [image_search].inference_base_url; "
            "configure it to a compatible SigLIP2 embedding service after startup"
        )
    if state == "legacy_v053" or (state == "fresh" and not dry_run):
        try:
            logger.info(
                "preparing bundled official providers schema_state={}", state
            )
            install_result = install_bundled_provider_plugins_once()
            logger.info(
                "bundled official providers ready installed={} "
                "already_completed={}",
                install_result.installed,
                install_result.already_completed,
            )
            if state == "legacy_v053":
                registrations = load_enabled_plugins(
                    settings.plugins,
                    root_dir=Path(settings.plugins.root_dir).expanduser(),
                )
                logger.info(
                    "v0.5.3 upgrade enabled plugins loaded registrations={} enabled={}",
                    len(registrations),
                    len(settings.plugins.enabled),
                )
                for provider_key in ("local", "cloud115"):
                    MEDIA_PROVIDER_REGISTRY.require(provider_key)
                    logger.info(
                        "v0.5.3 upgrade required provider available provider={}",
                        provider_key,
                    )
                logger.info("v0.5.3 upgrade official provider preparation completed")
        except Exception as exc:
            failures = {
                plugin_id: value
                for plugin_id, value in PLUGIN_LOAD_ERRORS.items()
                if plugin_id
                in {
                    "sakuramedia_local_provider",
                    "sakuramedia_115_provider",
                }
            }
            detail = f" errors={failures}" if failures else ""
            raise click.ClickException(
                f"官方存储插件预装或加载失败: {exc}{detail}"
            ) from exc
    else:
        logger.info(
            "v0.5.3 upgrade provider preparation skipped schema_state={}", state
        )
    try:
        logger.info("v0.5.3 upgrade invoking database bridge")
        summary = upgrade_v053_database(database, dry_run=dry_run)
    except LegacyV053UpgradeError as exc:
        raise click.ClickException(str(exc)) from exc
    if summary.upgraded:
        cleanup_legacy_v053_qdrant_collections()
    click.echo(
        "upgrade-v053 finished: "
        f"dry_run={str(dry_run).lower()} "
        f"upgraded={str(summary.upgraded).lower()} "
        f"media={summary.media_count} invalid_media={summary.invalid_media_count}"
    )
    logger.info(
        "v0.5.3 upgrade command finished upgraded={} media={} invalid_media={}",
        summary.upgraded,
        summary.media_count,
        summary.invalid_media_count,
    )


@main.command(name="wait-db")
@click.option(
    "--timeout",
    "timeout_seconds",
    type=float,
    default=60.0,
    show_default=True,
    help="Max seconds to wait for the database to accept connections.",
)
@click.option(
    "--interval",
    "interval_seconds",
    type=float,
    default=2.0,
    show_default=True,
    help="Seconds between connection attempts.",
)
def wait_db(timeout_seconds: float, interval_seconds: float):
    """落盘运行配置并等待 PostgreSQL 可连接"""
    import time

    from peewee import OperationalError

    from src.config.config import ensure_runtime_config
    from src.model.base import create_database

    ensure_runtime_config()
    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_error: OperationalError | None = None
    while True:
        attempt += 1
        database = create_database(settings.database)
        try:
            database.connect()
            database.execute_sql("SELECT 1")
            database.close()
            click.echo(f"database is ready (attempt {attempt})")
            return
        except OperationalError as exc:
            # 数据库容器尚未就绪（连接拒绝 / 启动中）；到截止时间前按固定间隔重试。
            last_error = exc
            if not database.is_closed():
                database.close()
        if time.monotonic() >= deadline:
            raise click.ClickException(
                f"database not ready after {timeout_seconds}s ({attempt} attempts): {last_error}"
            )
        time.sleep(interval_seconds)


@main.command(name="test-javdb")
@click.option("--movie-number", required=True, type=str, help="Movie number to query from JavDB.")
@click.option("--json", "output_json", is_flag=True, help="Print structured JSON output.")
def test_javdb(movie_number: str, output_json: bool):
    with _suppress_logs_for_json_output(output_json):
        if not output_json:
            logger.info("CLI test-javdb start movie_number={}", movie_number)
        provider = build_javdb_provider()
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


class _LazyApsGroup(click.Group):
    """APS 子命令组：首次调用时才从 JOB_REGISTRY 注册子命令。

    插件在 import 期会执行插件代码，插件管理 CLI
    （plugins list/install 等）不应为此被迫加载全部插件，因此注册表访问
    推迟到真正执行 APS 子命令时。
    """

    def invoke(self, ctx: click.Context) -> Any:
        if not self.commands:
            from src.scheduler.registry import JOB_REGISTRY

            for job_def in JOB_REGISTRY:
                _register_aps_command(job_def, group=self)
        return super().invoke(ctx)


@main.group(cls=_LazyApsGroup, invoke_without_command=True)
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
# 基于 JOB_REGISTRY 自动注册 APS 子命令；命令只负责提交队列
# ---------------------------------------------------------------------------


def _run_cli_job(job_def, params=None):
    from src.start.aps import run_job

    try:
        task_run = run_job(
            job_def,
            trigger_type="manual",
            params=params,
        )
    except TaskRunConflictError as exc:
        raise click.ClickException(str(exc))
    if task_run is None:
        raise click.ClickException(f"{job_def.cli_name} 未能入队")
    click.echo(
        f"{job_def.cli_name} queued: task_run_id={task_run.id} state={task_run.state}"
    )


def _register_aps_command(job_def, group):
    if job_def.manual_only and job_def.params_schema is None:
        # 无参的 manual_only 任务只能走 HTTP 触发，CLI 无法表达触发参数。
        return
    if job_def.params_schema is None:
        @group.command(name=job_def.cli_name, help=job_def.cli_help)
        def _cmd():
            _run_cli_job(job_def)

        return

    @group.command(name=job_def.cli_name, help=job_def.cli_help)
    @click.option(
        "--params-json",
        required=job_def.manual_only,
        help="任务参数 JSON，按任务声明的 params_schema 校验",
    )
    def _cmd_with_params(params_json):
        payload = None
        if params_json is not None:
            decoded = json.loads(params_json)
            if decoded is None:
                if job_def.manual_only:
                    raise click.BadParameter(
                        "当前任务的参数不能为 JSON null",
                        param_hint="--params-json",
                    )
            else:
                payload = job_def.params_schema.model_validate(decoded).model_dump()
        _run_cli_job(job_def, params=payload)


# ---------------------------------------------------------------------------
# 插件管理 CLI
# ---------------------------------------------------------------------------


@main.group(name="plugins")
def plugins_group():
    """插件管理（目录/zip 安装：list/install/remove/enable/disable/check）。"""


def _plugin_operation(operation):
    try:
        return operation()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@plugins_group.command("list")
def plugins_list():
    """列出已安装插件。"""
    for item in PluginManager().list_plugins():
        error = f" error={item['load_error']}" if item["load_error"] else ""
        click.echo(
            f"{item['plugin_id']:<24} {item['display_name']} "
            f"v{item['version']} enabled={str(item['enabled']).lower()} "
            f"load={item['load_status']}{error}"
        )


@plugins_group.command("install")
@click.argument("plugin_path", type=click.Path(exists=True, path_type=Path))
@click.option("--sha256", default=None, help="zip 包 sha256（可选，校验完整性）")
@click.option("--no-enable", is_flag=True, default=False, help="安装但不写入 enabled")
def plugins_install(plugin_path: Path, sha256: str | None, no_enable: bool):
    """把插件目录或 zip 包安装到插件根目录（重复安装保留 data/）。"""

    def _install():
        manager = PluginManager()
        if plugin_path.suffix.lower() == ".zip":
            return manager.install_zip(
                plugin_path, sha256=sha256, enable=not no_enable
            )
        return manager.install(plugin_path, enable=not no_enable)

    result = _plugin_operation(_install)
    restart_message = (
        "重启服务容器后同步依赖并生效"
        if PluginManager().pending_restart_for(result["plugin_id"]) == ["container"]
        else "重启 api 与 aps 后生效"
    )
    click.echo(
        f"插件 {result['plugin_id']} v{result['version']} 已安装；"
        f"{restart_message}"
    )


@plugins_group.command("remove")
@click.argument("plugin_id")
def plugins_remove(plugin_id: str):
    """删除插件代码并保留 data/；被媒体库使用的 provider 不可删除。"""
    _ensure_database_ready()
    _plugin_operation(lambda: PluginRemovalService.remove(plugin_id))
    click.echo(f"插件 {plugin_id} 已删除")


@plugins_group.command("enable")
@click.argument("plugin_id")
def plugins_enable(plugin_id: str):
    """启用插件（写入 enabled，重启后生效）。"""
    manager = PluginManager()
    _plugin_operation(lambda: manager.set_enabled(plugin_id, True))
    restart_message = (
        "重启服务容器后同步依赖并生效"
        if manager.pending_restart_for(plugin_id) == ["container"]
        else "重启 api 与 aps 后生效"
    )
    click.echo(f"插件 {plugin_id} 已启用；{restart_message}")


@plugins_group.command("disable")
@click.argument("plugin_id")
def plugins_disable(plugin_id: str):
    """停用插件（从 enabled 移除，重启后生效）。"""
    _plugin_operation(lambda: PluginManager().set_enabled(plugin_id, False))
    click.echo(f"插件 {plugin_id} 已停用；重启 api 与 aps 后生效")


@plugins_group.command("check")
@click.argument("plugin_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
def plugins_check(plugin_dir: Path):
    """校验插件目录（import + register + 契约），供插件作者使用。"""
    from src.plugins.loader import check_plugin_dir

    try:
        check_plugin_dir(plugin_dir=plugin_dir)
    except Exception as exc:
        raise click.ClickException(f"插件校验失败: {exc}") from exc
    click.echo(f"插件 {plugin_dir.name} 校验通过")


@plugins_group.command("sync-dependencies", hidden=True)
def plugins_sync_dependencies():
    """启动前同步已启用插件声明的依赖；失败由加载器隔离。"""
    from src.plugins.dependencies import sync_plugin_dependencies

    manager = PluginManager()
    failures = sync_plugin_dependencies(
        settings.plugins,
        root_dir=manager.root_dir,
    )
    for plugin_id, message in failures.items():
        click.echo(f"插件 {plugin_id} {message}")


@plugins_group.command("clear-field-owners")
@click.option("--plugin-id", required=True, type=str, help="解除接管的目标插件 id。")
@click.option(
    "--field",
    "fields",
    multiple=True,
    type=str,
    help="只清除指定字段的 owner（可重复）；不传则清除该插件全部字段 owner。",
)
def plugins_clear_field_owners(plugin_id: str, fields: tuple[str, ...]):
    """解除插件对 Movie 受保护字段的接管（插件被删除后其字段会冻结，用本命令释放回宿主）。"""
    from src.service.catalog.movie_ownership_gateway import MovieOwnershipGateway

    _ensure_database_ready()
    try:
        affected = MovieOwnershipGateway.release_plugin_owners(
            plugin_id,
            fields=fields if fields else None,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception:
        logger.exception("CLI plugins clear-field-owners crashed plugin_id={}", plugin_id)
        raise
    if fields:
        click.echo(f"已解除插件 {plugin_id} 对字段 {', '.join(fields)} 的接管，共 {affected} 行")
    else:
        click.echo(f"已解除插件 {plugin_id} 的全部字段接管，共 {affected} 行")


# ---------------------------------------------------------------------------
# 非 APS 命令（保持不变）
# ---------------------------------------------------------------------------


@main.command(name="reset-account")
@click.option(
    "--username",
    prompt="新账号用户名",
    type=str,
    help="重建后的单账号用户名（不传时交互式输入）。",
)
@click.option(
    "--password",
    prompt="新账号密码",
    hide_input=True,
    confirmation_prompt=True,
    type=str,
    help="重建后的单账号明文密码（不传时交互式隐式输入并二次确认）。",
)
def reset_account(username: str, password: str):
    """删除所有已有账号与 refresh token，按 --username/--password 重建单账号。

    忘记密码时使用；命令直接覆盖运行时账号，不校验旧密码。所有 refresh token
    一并清空，已登录客户端会立即失效需重新登录。
    """
    import bcrypt

    from src.model import User, UserRefreshToken
    from src.model.base import get_database

    normalized_username = username.strip()
    if not normalized_username:
        raise click.ClickException("username must not be blank")
    if not password:
        raise click.ClickException("password must not be blank")

    logger.info("CLI reset-account start username={}", normalized_username)
    _ensure_database_ready()

    password_hash = bcrypt.hashpw(
        password.encode("utf-8"), bcrypt.gensalt()
    ).decode("utf-8")

    # 事务内原子完成：先清空旧账号与所有 refresh token，再落新账号。
    # 中途失败整体回滚，避免出现"旧账号删了新账号没建"导致完全无法登录。
    with get_database().atomic():
        deleted_users = User.delete().execute()
        deleted_tokens = UserRefreshToken.delete().execute()
        new_user = User.create(
            username=normalized_username,
            password_hash=password_hash,
        )

    logger.info(
        "CLI reset-account finished user_id={} username={} deleted_users={} deleted_tokens={}",
        new_user.id,
        new_user.username,
        deleted_users,
        deleted_tokens,
    )
    click.echo(
        "account reset: "
        f"user_id={new_user.id} "
        f"username={new_user.username} "
        f"deleted_users={deleted_users} "
        f"deleted_refresh_tokens={deleted_tokens}"
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


if __name__ == "__main__":
    main()
