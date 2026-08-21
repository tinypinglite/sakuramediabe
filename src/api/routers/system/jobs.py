import peewee
from fastapi import APIRouter, Depends

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.model import BackgroundTaskRun
from src.scheduler.contracts import JobDefinition
from src.scheduler.registry import JOB_REGISTRY, JOB_REGISTRY_BY_KEY
from src.schema.system.activity import TaskRunResource
from src.schema.system.jobs import JobMetadataResource, ManualJobTriggerResponse
from src.service.system.activity_service import TaskRunConflictError
from src.start.aps import get_job_cron_setting, resolve_job_cron_expr, submit_manual_job

router = APIRouter(
    tags=["jobs"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


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


def _build_job_metadata(job_def: JobDefinition, last_run: BackgroundTaskRun | None) -> JobMetadataResource:
    return JobMetadataResource(
        task_key=job_def.task_key,
        log_name=job_def.log_name,
        cli_name=job_def.cli_name,
        cli_help=job_def.cli_help,
        plugin_id=job_def.plugin_id,
        cron_setting=get_job_cron_setting(job_def),
        cron_expr=resolve_job_cron_expr(job_def),
        manual_trigger_allowed=job_def.manual_trigger_allowed,
        params_schema=(
            job_def.params_schema.model_json_schema()
            if job_def.params_schema is not None
            else None
        ),
        last_task_run=TaskRunResource.model_validate(last_run) if last_run else None,
    )


@router.get("/system/jobs", response_model=list[JobMetadataResource])
def list_jobs():
    latest = _latest_task_run_by_key()
    return [_build_job_metadata(job_def, latest.get(job_def.task_key)) for job_def in JOB_REGISTRY]


@router.post("/system/jobs/{task_key}/run", response_model=ManualJobTriggerResponse)
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
        if job_def.service_factory is None:
            # handler-only 没有无参执行体，缺 body / JSON null 都不能创建必失败的队列行。
            raise ApiError(
                422,
                "invalid_job_params",
                f"任务 {task_key} 必须提供请求参数",
            )
    elif job_def.params_schema is None:
        # factory-only 不接受任何显式 body，避免调用方误以为参数会生效。
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
