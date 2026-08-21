import json
import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import click
from loguru import logger

import src.common.logging as app_logging
from src.api.exception.errors import ApiError
from src.common.logging import configure_logging
from src.config.config import settings
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import init_database
from src.model.enums import MediaLibraryBackend
from src.plugins.manager import PluginManager
from src.schema.playback.media_libraries import MediaLibraryCreateRequest
from src.service.catalog import MovieThinCoverBackfillService
from src.service.catalog.movie_asset_shard_migration_service import (
    MovieAssetShardMigrationService,
)
from src.service.catalog.movie_subtitle_unify_migration_service import (
    MovieSubtitleUnifyMigrationService,
)
from src.service.catalog.plot_layout_migration_service import PlotLayoutMigrationService
from src.service.playback import MediaLibraryService
from src.service.playback.jav_layout_migration_service import JavLayoutMigrationService
from src.service.system import TaskRunConflictError
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
    """等待 PostgreSQL 可连接；供容器启动时在迁移前对齐数据库就绪时序"""
    import time

    from peewee import OperationalError

    from src.model.base import create_database

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

    插件在 import 期加载有副作用（依赖安装、插件代码执行），插件管理 CLI
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
        # mixed 任务省略参数或显式 null 时保留 None，走全量 service_factory；
        # 显式传入非 null JSON（包括 '{}'）才调用 params_handler。
        payload = None
        if params_json is not None:
            decoded = json.loads(params_json)
            if decoded is None:
                # JSON null 仅对可走 factory 的非 manual_only 任务等同省略参数。
                if (
                    job_def.service_factory is None
                    and job_def.handler is None
                ) or job_def.manual_only:
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
    click.echo(
        f"插件 {result['plugin_id']} v{result['version']} 已安装；"
        "重启 api 与 aps 后生效"
    )


@plugins_group.command("remove")
@click.argument("plugin_id")
def plugins_remove(plugin_id: str):
    """删除插件目录（含 data/，请先自行备份）。"""
    _plugin_operation(lambda: PluginManager().remove(plugin_id))
    click.echo(f"插件 {plugin_id} 已删除")


@plugins_group.command("enable")
@click.argument("plugin_id")
def plugins_enable(plugin_id: str):
    """启用插件（写入 enabled，重启后生效）。"""
    _plugin_operation(lambda: PluginManager().set_enabled(plugin_id, True))
    click.echo(f"插件 {plugin_id} 已启用；重启 api 与 aps 后生效")


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
            MediaLibraryCreateRequest(
                name=name,
                backend=MediaLibraryBackend.LOCAL,
                backend_config={"root_path": root_path},
            )
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

    library_root_path = library.backend_config.get("root_path", "")
    logger.info(
        "CLI add-media-library finished library_id={} name={} root_path={}",
        library.id,
        library.name,
        library_root_path,
    )
    click.echo(
        "media library created: "
        f"library_id={library.id} "
        f"name={library.name} "
        f"root_path={library_root_path}"
    )


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


@main.command(name="migrate-jav-layout")
@click.option("--library-id", type=int, default=None,
              help="只迁移指定本地 library；缺省时处理全部本地媒体库。")
@click.option("--dry-run", is_flag=True, default=False,
              help="只统计不改任何东西，用于升级前预览规模。")
def migrate_jav_layout(library_id: int | None, dry_run: bool):
    """把本地媒体库的 JAV 从 <root>/番号/ 迁到 <root>/jav/番号/。

    单文件 4 步流程 + DB update 原子提交点，Ctrl+C / crash 后重跑可自动收敛：
    已达 F 的走 fast-path skip，中间态的会被恢复。
    """
    logger.info("CLI migrate-jav-layout start library_id={} dry_run={}", library_id, dry_run)
    _ensure_database_ready()

    def _progress(current: int, total: int, current_library_id: int) -> None:
        # 每 50 条打一次点，加上末尾一次；避免大库时 stderr 静默
        if current % 50 == 0 or current == total:
            click.echo(
                f"  library_id={current_library_id}  {current}/{total}",
                err=True,
            )

    stats = JavLayoutMigrationService.run(
        library_id=library_id,
        dry_run=dry_run,
        progress_callback=_progress,
    )
    payload = stats.to_dict()
    logger.info("CLI migrate-jav-layout finished dry_run={} stats={}", dry_run, payload)
    click.echo(
        "jav layout migrate finished "
        f"(dry_run={str(dry_run).lower()}): {payload}"
    )


@main.command(name="migrate-plot-layout")
@click.option("--dry-run", is_flag=True, default=False,
              help="只统计不改任何东西，用于升级前预览规模。")
def migrate_plot_layout(dry_run: bool):
    """把影片剧照从 movies/<num>/plots/N.ext 平铺成 movies/<num>/plot-N.ext。

    3 步流程 + image.origin 单条 UPDATE 原子提交点，Ctrl+C / crash 后重跑可自动收敛：
    已达 F 的走 fast-path（LIKE 查询天然不命中新格式），中间态的会被恢复。
    """
    logger.info("CLI migrate-plot-layout start dry_run={}", dry_run)
    _ensure_database_ready()

    def _progress(current: int, total: int) -> None:
        # 每 200 条打一次点，加上末尾一次；image 表规模比 media 大，节流粒度更粗。
        if current % 200 == 0 or current == total:
            click.echo(f"  {current}/{total}", err=True)

    stats = PlotLayoutMigrationService.run(
        dry_run=dry_run,
        progress_callback=_progress,
    )
    payload = stats.to_dict()
    logger.info("CLI migrate-plot-layout finished dry_run={} stats={}", dry_run, payload)
    click.echo(
        "plot layout migrate finished "
        f"(dry_run={str(dry_run).lower()}): {payload}"
    )


@main.command(name="migrate-movie-asset-shard")
@click.option("--dry-run", is_flag=True, default=False,
              help="只统计不改任何东西，用于升级前预览规模。")
def migrate_movie_asset_shard(dry_run: bool):
    """把影片资产从 movies/<番号>/ 分片成 movies/<sha1(番号)[:2]>/<番号>/。

    两阶段（整目录 rename 归片 + image.origin 前缀批量重写），Ctrl+C / crash 后重跑可自动收敛：
    已归片的目录不再出现在 movies/ 顶层天然跳过，DB 侧只重写老布局行。
    """
    logger.info("CLI migrate-movie-asset-shard start dry_run={}", dry_run)
    _ensure_database_ready()

    def _progress(phase: str, current: int, total: int) -> None:
        # 目录阶段 total 未知（-1，边扫边搬），image 阶段有确切总量；两阶段都节流打点。
        step = 50
        if current % step == 0:
            click.echo(f"  {phase} {current}/{total}", err=True)

    stats = MovieAssetShardMigrationService.run(
        dry_run=dry_run,
        progress_callback=_progress,
    )
    payload = stats.to_dict()
    logger.info("CLI migrate-movie-asset-shard finished dry_run={} stats={}", dry_run, payload)
    click.echo(
        "movie asset shard migrate finished "
        f"(dry_run={str(dry_run).lower()}): {payload}"
    )


@main.command(name="migrate-movie-subtitles")
@click.option("--dry-run", is_flag=True, default=False,
              help="只统计不改任何东西，用于升级前预览规模。")
def migrate_movie_subtitles(dry_run: bool):
    """把字幕从媒体库 sidecar 与旧字幕根统一收敛到 movies/<shard>/<番号>/subtitles/。

    单文件 3 步（link/copy -> UPDATE file_path -> unlink old），subtitle.file_path 单条 UPDATE
    是原子提交点，Ctrl+C / crash 后重跑可自动收敛。建议在 migrate-movie-asset-shard 之后运行。
    """
    logger.info("CLI migrate-movie-subtitles start dry_run={}", dry_run)
    _ensure_database_ready()

    def _progress(current: int, total: int) -> None:
        # 字幕表规模远小于 image，节流粒度更细。
        if current % 50 == 0 or current == total:
            click.echo(f"  {current}/{total}", err=True)

    stats = MovieSubtitleUnifyMigrationService.run(
        dry_run=dry_run,
        progress_callback=_progress,
    )
    payload = stats.to_dict()
    logger.info("CLI migrate-movie-subtitles finished dry_run={} stats={}", dry_run, payload)
    click.echo(
        "movie subtitle unify migrate finished "
        f"(dry_run={str(dry_run).lower()}): {payload}"
    )


if __name__ == "__main__":
    main()
