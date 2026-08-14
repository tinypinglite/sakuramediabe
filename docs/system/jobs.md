# 系统任务

系统任务接口提供 APScheduler 注册任务的元数据查询与受控手动触发能力，适合前端任务中心的“可执行任务列表”使用。

如果要查询任务运行历史、进度和通知，请继续使用：

- [任务中心与事件流接口](./task-runs.md)
- [通知中心接口](./notifications.md)

## 资源模型

### JobMetadataResource

```json
{
  "task_key": "movie_heat_update",
  "plugin_id": null,
  "log_name": "movie-heat-update",
  "cli_name": "update-movie-heat",
  "cli_help": "执行一次影片热度重算",
  "cron_setting": "movie_heat_cron",
  "cron_expr": "15 0 * * *",
  "manual_trigger_allowed": true,
  "params_schema": null,
  "last_task_run": {
    "id": 12,
    "task_key": "movie_heat_update",
    "task_name": "影片热度更新",
    "trigger_type": "scheduled",
    "state": "completed",
    "progress_current": null,
    "progress_total": null,
    "progress_text": null,
    "result_text": null,
    "result_summary": {"candidate_count": 120, "updated_count": 96, "formula_version": "v2"},
    "error_message": null,
    "started_at": "2026-05-13T02:00:00",
    "finished_at": "2026-05-13T02:03:12",
    "created_at": "2026-05-13T02:00:00",
    "updated_at": "2026-05-13T02:03:12"
  }
}
```

字段说明：

- `task_key`：任务稳定标识，与 `GET /system/task-runs` 中的 `task_key` 保持一致
- `plugin_id`：插件任务的来源 ID；内建任务为 `null`
- `log_name`：任务文件日志名
- `cli_name`：`python -m src.start.commands aps <cli_name>` 使用的命令名
- `cli_help`：CLI 帮助文案，也可作为前端说明文案
- `cron_setting`：内建任务为 `Scheduler` 字段名；插件任务为
  `plugins.job_crons.<plugin_id>.<task_key>` 配置路径
- `cron_expr`：当前运行配置解析出的 cron 表达式；缺失新增配置时回退默认值；
  `manual_only` 任务（无定时、只能手动带参触发）为 `null`
- `manual_trigger_allowed`：是否允许通过 HTTP 手动触发
- `params_schema`：任务声明了参数模型时返回其 JSON Schema，前端可据此渲染请求体；
  无参数任务为 `null`
- `last_task_run`：该任务最新一条运行记录；从未运行过时为 `null`

### ManualJobTriggerResponse

```json
{
  "task_run_id": 13,
  "task_key": "movie_heat_update",
  "state": "pending"
}
```

说明：

- 手动触发接口只负责创建任务记录并提交后台线程，不等待任务执行完成
- 返回的 `state` 通常是 `pending`；后续状态通过 `GET /system/task-runs`、`GET /system/activity/bootstrap` 或 SSE 获取

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/system/jobs` | 获取所有 APS 注册任务的元数据和最新运行记录 |
| `POST` | `/system/jobs/{task_key}/run` | 手动触发一个允许 HTTP 触发的 APS 注册任务 |

## `GET /system/jobs`

需要 Bearer Token。

成功响应：

- `200 OK`：返回任务元数据数组，顺序与后端任务注册表一致

错误响应：

- `401 Unauthorized`：未认证

行为说明：

- 只返回 `src.scheduler.registry.JOB_REGISTRY` 中注册的任务
- 只有在 `config.toml` 的 `plugins.enabled` 中显式启用且成功加载的插件任务才会进入注册表（插件开发见 [插件系统开发指南](./plugins.md)）
- `last_task_run` 使用每个 `task_key` 最新一条 `BackgroundTaskRun` 记录
- 任务元数据来自后端注册表，不允许通过接口修改

## `POST /system/jobs/{task_key}/run`

需要 Bearer Token。

路径参数：

- `task_key`：要触发的任务稳定标识

成功响应：

- `200 OK`：返回新建的任务运行记录 ID 与初始状态

请求体：

- 无参数任务：不传请求体；
- 声明了 `params_schema` 的任务：传符合该 JSON Schema 的 JSON 对象，
  例如字幕抓取任务 `{"movie_number": "ABP-123"}`。

错误响应：

- `401 Unauthorized`：未认证
- `403 manual_trigger_forbidden`：该任务不允许通过 HTTP 手动触发
- `422 invalid_job_params`：请求体不符合任务声明的 `params_schema`
- `404 job_not_found`：`task_key` 不在任务注册表中
- `409 task_conflict`：同一任务已有 `manual` 或 `scheduled` 运行记录占用互斥锁

冲突响应示例：

```json
{
  "error": {
    "code": "task_conflict",
    "message": "任务“影片热度更新”已在运行中",
    "details": {
      "blocking_task_run_id": 12,
      "blocking_trigger_type": "scheduled",
      "blocking_state": "running"
    }
  }
}
```

行为说明：

- 仅 `manual_trigger_allowed=true` 的任务允许通过 HTTP 触发
- HTTP 手动触发会创建 `trigger_type=manual` 的 `BackgroundTaskRun`
- 同一个 APS 注册任务在 `manual` 与 `scheduled` 之间按 `aps:<task_key>` 互斥
- 任务在线程内执行；API 进程退出会导致未完成任务中断，下一次启动或调度前会按任务恢复规则回收

## 前端接入建议

- 页面初始化先调用 `GET /system/jobs` 获取可执行任务列表和最新状态
- 点击手动执行后使用返回的 `task_run_id` 定位到任务详情或高亮对应运行记录
- 遇到 `409 task_conflict` 时展示正在运行的 `blocking_task_run_id`，不要重复提交
- 后续进度刷新复用 `GET /system/activity/bootstrap`、`GET /system/task-runs` 和 `GET /system/events/stream`
