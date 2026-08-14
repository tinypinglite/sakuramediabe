# 任务中心

任务中心提供后台任务的当前状态、进度、历史记录，以及资源级任务记录，适合 app 的任务中心页面直接消费。

如果你正在对接前端客户端，建议优先阅读：

- [前端资源任务对接说明](./frontend-resource-task-integration.md)

如果本次只对接新增的资源任务能力，建议先看：

- `GET /system/resource-task-states/definitions`
- `GET /system/resource-task-states`
- `POST /system/resource-task-actions`

如果是活动中心首屏，请优先使用：

- `GET /system/activity/bootstrap`

如果要展示可手动执行的 APS 任务列表，请使用：

- [系统任务接口](./jobs.md)

## 任务模型

- `task_key`
  - 任务类型稳定标识，如 `movie_heat_update`、`download_task_import`
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
  - 本轮失败次数：成功即清零、基础设施失败回滚；终身次数看 `resource_task_attempt` 表
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

### `GET /system/task-runs/{task_run_id}`

按 id 查询单条任务运行记录，响应结构与列表项一致（`TaskRunResource`）。

说明：

- 主要用途：手动触发接口（影片翻译/互动同步、资源任务 action）返回 202 与 `task_run_id`
  后，前端在页面刷新、SSE 断连等丢失事件游标的场景下，仅凭 `task_run_id` 追溯终态与
  `error_message`
- 记录不存在或已被活动清理任务删除时返回 404（`task_run_not_found`），前端按
  "记录已过期"处理

### `GET /system/resource-task-states/definitions`

返回所有已注册资源任务定义，供前端渲染任务切换 Tab。

返回字段：

- `task_key`
- `resource_type`
- `display_name`
- `default_sort`
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

媒体缩略图失败列表直接复用本接口：

```http
GET /system/resource-task-states?task_key=media_thumbnail_generation&state=failed_retryable&page=1&page_size=20&sort=last_error_at:desc
```

返回记录的 `resource_id` 即 `media_id`；`resource` 摘要同时包含影片番号、标题、媒体路径和有效状态。

### `POST /system/resource-task-actions`

资源级操作的唯一入口。旧的任务专用端点（缩略图 reset、订阅 search-resets、影片页单片
翻译/互动同步）均已删除，全部并入本协议。

请求体：

```json
{
  "task_key": "media_thumbnail_generation",
  "action": "reset_retry_budget",
  "resource_ids": [101, 102, 103]
}
```

action 枚举：

- `retry_now`：`failed_retryable` / `exhausted` → 立即重试（`exhausted` 隐式重开预算），
  入队一个带 `only_ids` 的可跟踪 run，响应携带 `task_run_id`
- `rerun`：通用"立即强制执行"入口，`running` 之外任意状态可用；从未记账的资源就地播种
  状态行；子集运行绕过"已完成"域条件（重译已翻译影片、强制补刷互动数都走它）
- `reset_retry_budget`：`failed_retryable` / `failed_terminal` / `exhausted` → 回
  `pending` 重开预算（`retry_round + 1`、本轮计数归零），不建 run，等下一轮定时任务处理

约束与语义：

- 每条记录实际可用的 action 以 `available_actions` 为准 = 状态可用集 ∩ 任务声明支持集
  （definitions 接口的 `supported_actions`）；`media_thumbnail_generation` 与
  `subscribed_movie_auto_download` 不开放 `rerun`
- `resource_ids` 一次最多 `500` 个唯一正整数 ID；部分成功语义，不合格 ID 逐条记入
  `skipped` 并给出原因
- `resource_ids` 缺省时按 `state` 圈定整批目标，仅 `reset_retry_budget` 支持
  （例：`{"task_key": "subscribed_movie_auto_download", "action": "reset_retry_budget",
  "state": "exhausted"}` 即旧订阅页"重置全部已放弃"）；`state` 缺省为三个失败态全部
- 领域合格性前置：影片已删除（`movie_not_found`）、缺原始简介（`movie_desc_missing`）、
  缺 JavDB ID（`movie_javdb_id_missing`）、未订阅（`movie_not_subscribed`）、媒体已删除
  （`media_not_found`）、媒体失效（`media_invalid`）等由任务注册的钩子逐条跳过
- 协议级跳过原因：`task_state_not_found`（retry_now / reset 要求已有状态行；rerun 会播种）、
  `state_not_actionable`（当前状态不允许该 action）
- 连点去重：单资源操作互斥到 `resource_action:{task_key}:{resource_id}`（不同资源互不
  阻塞），多资源操作互斥到 `resource_action:{task_key}`，冲突返回 `409`
  （`resource_task_action_conflict`，携带 `blocking_task_run_id`）

成功响应：

```json
{
  "task_key": "media_thumbnail_generation",
  "action": "reset_retry_budget",
  "task_run_id": null,
  "accepted_resource_ids": [101, 102],
  "skipped": [
    {
      "resource_id": 103,
      "reason": "media_invalid"
    }
  ]
}
```

`retry_now` / `rerun` 的 `task_run_id` 指向入队的 run，前端凭它经
`GET /system/task-runs/{task_run_id}` 或 SSE 事件跟进终态；`reset_retry_budget` 恒为 `null`。
重置不清尝试历史（attempt 表保留），只重开预算并归位投影快照。

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
- 批量媒体秒传任务（`media_rapid_upload`）：批次内顺序执行，结束后创建一条汇总通知

## 影片描述回填终态失败

- `movie_desc_sync` 在 DMM 明确返回“未找到对应番号”时，会把记录写成 `failed_terminal`
  （`error_code` 结构化标注，Wave 2 起取代 `extra.terminal`）
- 这类记录不会再被自动调度重复抓取；可经统一 action 的 `reset_retry_budget` / `rerun` 捞回

## APS 手动与定时互斥

- 同一个 APS 注册任务在 `trigger_type = manual` 与 `trigger_type = scheduled` 之间按 `task_key` 互斥
- 手动执行 `aps <job>` 时，如果同任务已经在运行，命令会直接报错退出，不会新增任务记录
- 定时触发时，如果同任务已经在运行，本次调度会直接跳过并写日志，不会新增伪任务记录
- `startup` 与 `internal` 任务不参与这条互斥规则，保持各自现有行为

## 记录保留与清理

这三张表只写不删、会随每次任务运行无界增长，由 `activity_record_cleanup` 任务（CLI `aps cleanup-activity-records`，默认 `30 5 * * *`）按保留期回收，清理逻辑见 `ActivityCleanupService`：

- `system_event`：仅服务于在线 SSE 增量推送，历史事件无回放路径，只保留最近 `activity_event_retention_days`（默认 1）天，更旧的删除。
- `background_task_run`：每个 `task_key` 只保留最近 `activity_task_run_retention_per_key`（默认 200）条运行记录供翻页，更旧的删除；删除前会把指向它的通知外键 `related_task_run` 显式置空，避免悬挂引用。
- `system_notification`：已读且 `read_at` 超过 `activity_notification_read_retention_days`（默认 3）天的删除，未读一律保留。

> 注意：清理任务只控制后续增长，不会自动回收 PostgreSQL 已占用的磁盘；存量积压清掉后如需释放空间需另行 `VACUUM`。

## 中断恢复规则

- `trigger_type = scheduled` 的任务会在 `aps` 进程启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- `trigger_type = manual` 的任务会在 `aps` 与 API 启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- `trigger_type = internal` 的任务会在 `aps` 与 API 启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- `trigger_type = startup` 的任务会在 API 启动时扫描旧的 `pending` / `running` 记录，并统一回收为 `failed`
- 当回收到 `movie_desc_sync`、`movie_interaction_sync`、`movie_desc_translation`、`movie_title_translation` 或 `media_thumbnail_generation` 时，会联动把对应 `resource_task_state.state = running` 回收为 `failed`
- 当回收到 `download_task_import` 任务时，会联动执行孤儿导入恢复；基于 `ImportJob` 与运行器活跃状态一起判定，只有确认导入线程已经失活，才会把 `ImportJob`、`DownloadTask.import_status` 与对应 activity 状态统一回收为失败链路
- 当回收到 `media_rapid_upload` 时，会释放逐媒体活动锁；尚未切换云端定位的条目标记为 `failed`，已经切换云端但未完成本地清理的条目标记为 `cleanup_failed`，供重试接口继续收敛
