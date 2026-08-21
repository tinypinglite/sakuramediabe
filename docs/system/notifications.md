# 通知中心

通知中心提供后台活动消息流，适合 app 的通知中心页面直接消费。

如果你正在对接前端客户端，建议优先阅读：

- [前端资源任务对接说明](./frontend-resource-task-integration.md)

如果是活动中心首屏，请优先使用：

- `GET /system/activity/bootstrap`

## 通知模型

- `category`
  - `reminder`：提醒（业务侧需要用户感知的事件，如新影片可播放）
  - `info`：普通信息（当前无后台任务自动触发，仅保留作为合法分类）
  - `warning`：警告（任务完成但带 failed 统计；skipped 等正常跳过不触发）
  - `error`：错误（任务失败）
- `is_read`
  - 已读状态
- `event_type` / `resource_type` / `resource_id`
  - 可选的机器可读事件及其资源身份；不替代旧的 `related_resource_*` 展示关联
- `dedupe_key`
  - 可选的稳定幂等键，非空时全局唯一；同一键重复投递会复用原通知，不会新增重复记录。
    历史通知保持空值，不按标题或正文回填、合并。

## 接口

### `GET /system/notifications`

查询参数：

- `page`
- `page_size`
- `category`
- `is_read`

说明：

- 这个接口继续用于筛选、分页和加载更多
- 活动中心首屏不要再并行拼这个接口，改走 `GET /system/activity/bootstrap`

### `PATCH /system/notifications/{notification_id}/read`

把通知标记为已读。

### `POST /system/notifications/read`

批量把指定的多条通知标记为已读。请求体 `{ "ids": [1, 2, 3] }`，返回 `{ updated_count, unread_count }`：

- `updated_count`：本次新置为已读的条数（已读或不存在的 ID 自动忽略）
- `unread_count`：操作后剩余未读数

`ids` 为空时为 no-op（返回 `updated_count=0`）。成功响应中的 `unread_count` 是操作后的数据库快照；其它在线页面通过轮询通知列表同步未读态。

### `POST /system/notifications/read-all`

把当前所有未读通知一次性标记为已读。无请求体，返回 `{ updated_count, unread_count }`：

- `updated_count`：本次新置为已读的通知条数
- `unread_count`：操作后剩余未读数（正常为 0）

没有未读时返回 `updated_count=0`。成功响应中的 `unread_count` 正常为 `0`，其它在线页面通过轮询通知列表同步未读角标。

## 说明

- 后端任务常态成功不再生成通知（避免高频任务刷屏）；仅当成功但 `result_summary` 含 `failed` 计数（>0）时才生成一条 `warning` 通知，`skipped` 等其它指标不触发
- 后端任务失败后会生成一条 `error` 通知
- 下载导入任务新增可播放影片时，会额外生成一条 `reminder` 通知
- Cloud115 登录失效、离线任务放弃、秒传批次完成和下载导入提醒均按稳定事件键去重
- 活动中心正确接入方式是“bootstrap 首屏快照 + 通知/任务列表按需轮询”
