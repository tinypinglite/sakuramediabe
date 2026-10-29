from datetime import timedelta

import peewee
from fastapi import APIRouter, Depends, status

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.common.runtime_time import utc_now_for_db
from src.model import BackgroundTaskRun
from src.plugins.i18n import localize_json_schema
from src.scheduler.contracts import JobDefinition
from src.scheduler.registry import JOB_REGISTRY, JOB_REGISTRY_BY_KEY
from src.schema.system.jobs import JobMetadataResource, ManualJobTriggerResponse
from src.service.system.activity import TaskRunConflictError, TaskRunService
from src.service.system.activity.task_text import resolve_job_cli_help
from src.service.system.optional_services import job_disabled_reason
from src.start.aps import get_job_cron_setting, resolve_job_cron_expr, submit_manual_job

router = APIRouter(
    tags=["jobs"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)

RECENT_JOB_RUN_WINDOW_HOURS = 24


def _latest_task_run_by_key() -> dict[str, BackgroundTaskRun]:
    # 每个 task_key 只取最新一条 task_run：先用子查询按 task_key 分组取最大 id
    # （id 自增，最大即最新），再按主键回表。这样数据库只返回任务个数那么多行，
    # 避免把整张 background_task_run 历史全部拉回进程后再在 Python 里去重。
    keys = list(JOB_REGISTRY_BY_KEY.keys())
    latest_ids = (
        BackgroundTaskRun.select(peewee.fn.MAX(BackgroundTaskRun.id))
        .where(BackgroundTaskRun.task_key.in_(keys))
        .group_by(BackgroundTaskRun.task_key)
    )
    rows = BackgroundTaskRun.select().where(BackgroundTaskRun.id.in_(latest_ids))
    return {row.task_key: row for row in rows}


def _recent_job_run_stats() -> dict[str, tuple[int, int]]:
    # 近 24 小时按 task_key 聚合运行数与失败数；只统计任务注册表里的键，
    # 免得历史遗留或插件残留的 task_key 混进任务列表口径。
    keys = list(JOB_REGISTRY_BY_KEY.keys())
    cutoff = utc_now_for_db() - timedelta(hours=RECENT_JOB_RUN_WINDOW_HOURS)
    rows = (
        BackgroundTaskRun.select(
            BackgroundTaskRun.task_key,
            peewee.fn.COUNT(BackgroundTaskRun.id).alias("run_count"),
            peewee.fn.SUM(
                peewee.Case(None, [(BackgroundTaskRun.state == "failed", 1)], 0)
            ).alias("failure_count"),
        )
        .where(
            BackgroundTaskRun.task_key.in_(keys),
            BackgroundTaskRun.created_at >= cutoff,
        )
        .group_by(BackgroundTaskRun.task_key)
    )
    return {
        row.task_key: (row.run_count, int(row.failure_count or 0))
        for row in rows
    }


def _build_job_metadata(
    job_def: JobDefinition,
    last_run: BackgroundTaskRun | None,
    recent_stats: tuple[int, int],
) -> JobMetadataResource:
    disabled_reason = job_disabled_reason(job_def.task_key)
    recent_run_count, recent_failure_count = recent_stats
    return JobMetadataResource(
        task_key=job_def.task_key,
        log_name=job_def.log_name,
        cli_name=job_def.cli_name,
        cli_help=resolve_job_cli_help(job_def),
        plugin_id=job_def.plugin_id,
        cron_setting=get_job_cron_setting(job_def),
        cron_expr=resolve_job_cron_expr(job_def),
        disabled_reason=disabled_reason,
        manual_trigger_allowed=job_def.manual_trigger_allowed and not disabled_reason,
        params_schema=(
            localize_json_schema(job_def.params_schema.model_json_schema())
            if job_def.params_schema is not None
            else None
        ),
        recent_run_count=recent_run_count,
        recent_failure_count=recent_failure_count,
        # 统一走 TaskRunService 序列化，last_task_run 也带 has_warning。
        last_task_run=TaskRunService.to_task_run_resource(last_run) if last_run else None,
    )


@router.get("/system/jobs", response_model=list[JobMetadataResource])
def list_jobs():
    latest = _latest_task_run_by_key()
    recent = _recent_job_run_stats()
    return [
        _build_job_metadata(
            job_def,
            latest.get(job_def.task_key),
            recent.get(job_def.task_key, (0, 0)),
        )
        for job_def in JOB_REGISTRY
    ]


@router.post(
    "/system/jobs/{task_key}/runs",
    response_model=ManualJobTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_job(task_key: str, payload: dict | None = None):
    job_def = JOB_REGISTRY_BY_KEY.get(task_key)
    if job_def is None:
        raise ApiError(404, "job_not_found", f"未知任务 task_key={task_key}")
    if not job_def.manual_trigger_allowed:
        raise ApiError(
            403,
            "manual_trigger_forbidden",
            f"任务 {task_key} 不允许通过接口手动触发",
        )

    params = None
    if payload is None:
        if job_def.manual_only and job_def.params_schema is not None:
            # 声明参数模型的 manual_only 任务必须显式提供参数；无参任务可直接入队。
            raise ApiError(
                422,
                "invalid_job_params",
                f"任务 {task_key} 必须提供请求参数",
            )
    elif job_def.params_schema is None:
        # 无参数任务不接受显式 body，避免调用方误以为参数会生效。
        raise ApiError(
            422,
            "invalid_job_params",
            f"任务 {task_key} 不支持请求参数",
        )
    else:
        try:
            # 显式 JSON 对象严格按 schema 校验；空对象同样代表一次带参调用。
            params = job_def.params_schema.model_validate(payload).model_dump()
        except Exception as exc:
            raise ApiError(
                422,
                "invalid_job_params",
                f"任务 {task_key} 参数校验失败",
                {"detail": str(exc)},
            ) from exc

    try:
        task_run = submit_manual_job(job_def, params=params)
    except TaskRunConflictError as exc:
        blocking = exc.blocking_task_run
        raise ApiError(
            409,
            "task_conflict",
            str(exc),
            details={
                "blocking_task_run_id": blocking.id,
                "blocking_trigger_type": blocking.trigger_type,
                "blocking_state": blocking.state,
            },
        ) from exc

    return ManualJobTriggerResponse(
        task_run_id=task_run.id,
        task_key=task_key,
        state=task_run.state,
    )
