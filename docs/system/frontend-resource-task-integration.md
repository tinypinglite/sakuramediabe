# 前端任务中心对接

后台任务界面以 Jobs、TaskRun 和通知为主；缩略图失败列表应额外读取 `GET /media?thumbnail_generation_state=terminal`，不从历史 TaskRun 反推。

## 页面初始化

1. 调用 `GET /system/jobs` 获取任务定义、当前 cron、是否允许手动执行、参数 JSON Schema 和最近一次运行。
2. 调用 `GET /system/activity/bootstrap` 获取首屏通知、未读数、活动任务和最近运行。
3. 页面可见期间轮询 `GET /system/task-runs` 与 `GET /system/notifications`；页面隐藏后停止轮询。

## 手动执行

调用：

```http
POST /system/jobs/{task_key}/run
```

内建任务一律省略请求体，按任务自身的候选规则执行整批。只有插件主动声明 `params_schema` 时，前端才渲染并提交参数表单。

列表项会返回 `thumbnail_generation_state` 与 `thumbnail_last_error_code`；订阅搜索状态、次数和错误信息由 `/movie-subscriptions` 返回。

成功响应返回 `task_run_id` 和初始 `pending` 状态。前端据此高亮对应运行记录；遇到 `409 task_conflict` 时展示响应中的占用任务，不重复提交。

## 结果展示

- 进度使用 `progress_current`、`progress_total` 和 `progress_text`。
- 成功摘要使用 `result_text` 和 `result_summary`。
- 失败使用 `error_message`；任务内部分资源失败时，读取该任务 `result_summary` 中的计数和失败明细。
- 通知通过 `related_task_run_id` 跳转到对应运行记录。

订阅资源状态直接读取订阅接口的领域状态；下载、导入和媒体状态分别读取其业务接口，不从任务运行记录反推。
