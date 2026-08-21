# 任务中心

任务中心以 `BackgroundTaskRun` 为唯一运行记录，提供任务进度、结果摘要和失败信息。任务的可执行定义与手动触发见[系统任务](./jobs.md)，通知见[通知中心](./notifications.md)。

## 任务模型

`TaskRunResource` 主要字段：

- `id`、`task_key`、`task_name`
- `trigger_type`：`scheduled`、`manual`、`startup` 或 `internal`
- `state`：`pending`、`running`、`completed` 或 `failed`
- `progress_current`、`progress_total`、`progress_text`
- `result_text`、`result_summary`、`error_message`
- `started_at`、`finished_at`、`created_at`、`updated_at`

任务的候选资源和失败明细由各任务的 `result_summary` 表达。缩略图状态归 `Media`；订阅资源查询状态归 `Movie` 并通过 `/movie-subscriptions` 查看；TaskRun 只记录本轮执行摘要。

## `GET /system/task-runs`

需要 Bearer Token。查询参数：

- `page`、`page_size`
- `state`
- `task_key`
- `trigger_type`
- `sort`

接口返回 `PageResponse<TaskRunResource>`，用于筛选、分页和加载历史记录。

## `GET /system/activity/bootstrap`

活动中心首屏使用此接口一次取得：

- 通知分页与未读数
- 当前活动任务
- 最近任务运行分页

首屏之后按页面可见性轮询 `GET /system/task-runs` 与 `GET /system/notifications`。任务进度以当前 `background_task_run` 行为准。

## 手动执行

先调用 `GET /system/jobs` 获取任务定义和 `params_schema`，再调用：

```http
POST /system/jobs/{task_key}/run
```

内建维护任务均可省略请求体，按其常规候选规则执行整批。插件声明了参数模型时，才按其公开 schema 提交请求体。响应中的 `task_run_id` 用于后续轮询。

## 保留与恢复

- 每个 `task_key` 的已完成/失败记录按 `activity_task_run_retention_per_key` 保留；活动中的 `pending`、`running` 记录不参与终态裁剪。
- 持久队列中的定时任务可以跨进程重启继续等待领取。
- worker 通过租约识别中断执行，失败原因写入运行记录；业务资源是否需要再次处理，由各领域状态决定。文件巡检或任一导入链路复活旧 Media、以及内容版本改变会自动重新进入缩略图候选。
