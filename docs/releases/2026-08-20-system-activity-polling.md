# 系统活动改为轮询

> 破坏性变化：系统活动的通知与任务进度不再通过持久化 SSE 事件流推送。

## 数据库升级边界

> v0.5.0 是破坏性版本：数据库升级只支持 `v0.4.21 -> v0.5.0`，不支持从其它历史版本直接升级。

- v0.5.0 将历史迁移收敛为一条迁移；旧版本迁移文件不再随版本发布。
- 全新数据库仍可直接初始化；这不属于旧库升级路径。
- v0.4.21 的 `schema_migration` 记录会保留作审计，新迁移会在其上记录当前收敛版本。

## 调用方需要调整

- `GET /system/events/stream` 已删除；不要再建立对应的 `EventSource` 连接。
- 活动中心首屏继续使用 `GET /system/activity/bootstrap`。
- 后续通知变化轮询 `GET /system/notifications`，任务进度与终态轮询
  `GET /system/task-runs`；页面不可见时应停止轮询。

JavDB 搜索/导入等操作型 SSE 保持原有行为。下载任务的
`GET /download-tasks/stream` 同步移除，调用方改为轮询 `GET /download-tasks`；
qB 进度由 APS 内部采样器写入任务快照字段。

## 订阅列表响应精简

> 破坏性变化：`GET /movie-subscriptions` 的列表项不再返回
> `import_operation`。

前端需要移除订阅行内的导入计数、失败原因和快捷操作。`import_failed` 分类保留，
仍由下载任务的导入状态判定；具体执行记录统一在任务中心查看和管理。

## 字幕导入改用 TaskRun

> 破坏性变化：字幕导入的独立作业模型和管理端点已删除。

- `POST /subtitle-imports` 保留，响应改为 `task_run_id` / `task_key` / `state`；进度与结果通过
  `GET /system/task-runs` 轮询
- `GET /subtitle-imports`、作业详情以及失败文件重试/删除/改名端点已删除
- 单文件失败只保留日志和汇总计数。修正源目录后，重新 POST 整个目录即可
- 升级迁移会删除 `subtitle_import_job` 表，已有 pending/running 旧作业直接放弃，
  不做自动转换；需由用户重新提交源目录
- v0.5.0 不再保留旧的 `SubtitleImportJob` 模型定义；旧表仅由收敛迁移按表名处理
