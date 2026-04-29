# 任务中心

任务中心提供后台任务的当前状态、进度、历史记录，以及资源级任务记录，适合 app 的任务中心页面直接消费。

如果你正在对接前端客户端，建议优先阅读：

- [前端资源任务对接说明](./frontend-resource-task-integration.md)

如果本次只对接新增的资源任务能力，建议先看：

- `GET /system/resource-task-states/definitions`
- `GET /system/resource-task-states`
- `POST /system/resource-task-states/{task_key}/{resource_id}/reset`

如果是活动中心首屏，请优先使用：

- `GET /system/activity/bootstrap`

## 任务模型

- `task_key`
  - 任务类型稳定标识，如 `ranking_sync`、`download_task_import`
- `task_name`
  - 前端直接展示的任务名称
- `trigger_type`
  - `scheduled`
  - `manual`
  - `startup`
  - `internal`
- `state`
  - `pending`
  - `running`
  - `completed`
  - `failed`
- `progress_current`
- `progress_total`
- `progress_text`
- `result_text`
- `result_summary`
- `error_message`

## 资源任务记录模型

资源级任务状态统一存放在 `resource_task_state`，用于“任务记录页”直接按任务查看每条资源的执行状态。

- `task_key`
  - 稳定任务标识，如 `movie_desc_sync`、`media_thumbnail_generation`
- `resource_type`
  - 当前支持 `movie`、`media`
- `resource_id`
  - 资源主键
- `state`
  - `pending`
  - `running`
  - `succeeded`
  - `failed`
- `attempt_count`
- `last_attempted_at`
- `last_succeeded_at`
- `last_error`
- `last_error_at`
- `last_task_run_id`
- `last_trigger_type`
- `resource`
  - 任务页补充的资源摘要
  - `movie` 任务返回 `movie_number`、`title`
  - `media` 任务额外返回 `path`、`valid`

## 接口

### `GET /system/task-runs`

查询参数：

- `page`
- `page_size`
- `state`
- `task_key`
- `trigger_type`
- `sort`

默认排序：`started_at:desc`

说明：

- 这个接口继续用于筛选、分页和加载更多
- 活动中心首屏不要再并行拼这个接口，改走 `GET /system/activity/bootstrap`

### `GET /system/task-runs/active`

只返回 `pending` / `running` 任务。

说明：

- 这个接口继续保留给独立任务面板或非首屏刷新使用
- 活动中心首屏不要再单独请求它，改由 bootstrap 一次返回
- `active` 的语义是“系统当前确认仍活跃的任务”；如果后台进程或线程已经中断，启动恢复逻辑会把旧任务改成 `failed`

### `GET /system/resource-task-states/definitions`

返回所有已注册资源任务定义，供前端渲染任务切换 Tab。

返回字段：

- `task_key`
- `resource_type`
- `display_name`
- `default_sort`
- `allow_reset`
- `state_counts`
  - `pending`
  - `running`
  - `succeeded`
  - `failed`

当前已注册：

- `movie_desc_sync`
- `movie_interaction_sync`
- `movie_desc_translation`
- `movie_title_translation`
- `media_thumbnail_generation`

### `GET /system/resource-task-states`

查询指定任务的资源级记录分页。

查询参数：

- `task_key`：必填
- `page`
- `page_size`
- `state`
- `search`
  - `movie` 任务按 `movie_number`、`title`、`javdb_id` 搜索
  - `media` 任务按 `movie_number`、`title`、`path` 搜索
- `sort`

允许排序：

- `last_attempted_at:desc`
- `last_attempted_at:asc`
- `last_error_at:desc`
- `attempt_count:desc`
- `updated_at:desc`
- `updated_at:asc`

说明：

- 未传 `sort` 时使用任务定义里的 `default_sort`
- 这个接口只返回已落到 `resource_task_state` 的记录
- 前端可结合 `last_task_run_id` 跳转到批次级任务详情

### `POST /system/resource-task-states/{task_key}/{resource_id}/reset`

把一条失败记录重置回 `pending`，供下次调度重新纳入候选。

重置语义：

- 仅允许当前 `state == failed` 的记录
- `state` 重置为 `pending`
- `attempt_count` 重置为 `0`
- 清空 `last_error`、`last_error_at`
- `last_trigger_type` 记为 `manual`
- `last_task_run_id` 清空
- 保留 `last_attempted_at`、`last_succeeded_at` 作为历史痕迹
- 如果任务使用 `extra.terminal = true` 表示终态失败，reset 会同时清掉这个标记

### `GET /system/events/stream`

返回 `text/event-stream`，用于在线场景的增量刷新。

说明：

- SSE 只负责 bootstrap 之后的增量补追
- 活动中心页面初始化不要再用 `after_event_id=0` 追整段历史

事件类型：

- `task_run_created`
- `task_run_updated`
- `notification_created`
- `notification_updated`
- `heartbeat`

## 已接入任务

- APScheduler 注册的后台任务
- 下载完成后的异步导入任务
- 影片简介翻译任务（`movie_desc_translation`）
- 影片标题翻译任务（`movie_title_translation`）

## 影片描述回填终态失败

- `movie_desc_sync` 在 DMM 明确返回“未找到对应番号”时，会把记录写成 `failed` 且 `extra.terminal = true`
- 这类记录不会再被自动调度重复抓取
- 如果后续需要人工重新尝试，可以调用 reset 清掉终态标记，再重新进入候选

## APS 手动与定时互斥

- 同一个 APS 注册任务在 `trigger_type = manual` 与 `trigger_type = scheduled` 之间按 `task_key` 互斥
- 手动执行 `aps <job>` 时，如果同任务已经在运行，命令会直接报错退出，不会新增任务记录
- 定时触发时，如果同任务已经在运行，本次调度会直接跳过并写日志，不会新增伪任务记录
- `startup` 与 `internal` 任务不参与这条互斥规则，保持各自现有行为

## 中断恢复规则

- `trigger_type = scheduled` 的任务会在 `aps` 进程启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- `trigger_type = manual` 的任务会在 `aps` 与 API 启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- `trigger_type = internal` 的任务会在 `aps` 与 API 启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- `trigger_type = startup` 的任务会在 API 启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- 当回收到 `movie_desc_sync`、`movie_interaction_sync`、`movie_desc_translation`、`movie_title_translation` 或 `media_thumbnail_generation` 时，会联动把对应 `resource_task_state.state = running` 回收为 `failed`
- 当回收到 `download_task_import` 任务时，会联动执行孤儿导入恢复；基于 `ImportJob` 与运行器活跃状态一起判定，只有确认导入线程已经失活，才会把 `ImportJob`、`DownloadTask.import_status` 与对应 activity 状态统一回收为失败链路
