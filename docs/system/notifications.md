# 通知中心

通知中心提供后台活动消息流，适合 app 的通知中心页面直接消费。

如果你正在对接前端客户端，建议优先阅读：

- [前端资源任务对接说明](./frontend-resource-task-integration.md)

如果是活动中心首屏，请优先使用：

- `GET /system/activity/bootstrap`

## 通知模型

- `category`
  - `reminder`：提醒（业务侧需要用户感知的事件，如新影片可播放）
  - `info`：普通（任务正常完成）
  - `warning`：警告（任务完成但带 failed/skipped 统计）
  - `error`：错误（任务失败）
- `is_read`
  - 已读状态
- `archived`
  - 是否已归档；默认列表只返回未归档通知

## 接口

### `GET /system/notifications`

查询参数：

- `page`
- `page_size`
- `category`
- `is_read`
- `archived`

默认 `archived=false`。

说明：

- 这个接口继续用于筛选、分页和加载更多
- 活动中心首屏不要再并行拼这个接口，改走 `GET /system/activity/bootstrap`

### `PATCH /system/notifications/{notification_id}/read`

把通知标记为已读。

## 说明

- 后端任务成功完成后会生成一条 `info` 通知；若 `result_summary` 含 `failed`/`skipped` 等指标则升级为 `warning`
- 后端任务失败后会生成一条 `error` 通知
- 下载导入任务新增可播放影片时，会额外生成一条 `reminder` 通知
- 活动中心正确接入方式是“bootstrap 首屏快照 + SSE 增量续传”
