# Movie Subscriptions

## 资源说明

影片订阅管理：查看所有已订阅影片及其资源查询进展，以及手动重置资源查询状态让放弃的影片回到队列。

**订阅是长期意图标记**：影片入库之后订阅照常保留，不会自动解除，因此订阅列表只增不减。
前端默认视图应当是「需要关注的」（`missing` / `exhausted` / `failed`），全部订阅放次要位置。

订阅的写入侧（订阅 / 取消订阅）在 [movies.md](./movies.md)：
`PUT|DELETE /movies/{movie_number}/subscription`、`POST /movies/subscriptions`、
`POST /movies/unsubscriptions`。**批量取消订阅就用那边的 `POST /movies/unsubscriptions`**，本域
不另造一套；要连本地媒体文件一起删的走 `DELETE /media/{media_id}`。

资源查询本身的行为（没找到次数与放弃、死种判定、选种黑名单）见
[transfers/downloads.md](../transfers/downloads.md) 的「内部定时任务」。

## 资源模型

```json
{
  "movie_id": 123,
  "movie_number": "ABP-123",
  "title": "…",
  "cover_image": { "id": 1, "origin": "…", "small": "…", "medium": "…", "large": "…" },
  "release_date": "2019-05-01",
  "subscribed_at": "2026-01-02T03:04:05",
  "status": "missing",
  "is_fresh": false,
  "attempt_count": 2,
  "attempt_limit": 3,
  "last_searched_at": "2026-07-20T02:30:00",
  "last_error": null,
  "dead_download_task_count": 1,
  "media_count": 0
}
```

字段说明：

- `movie_id`：统一 action 协议（`POST /system/resource-task-actions` 的 `resource_ids`）
  的操作主键，前端选择态以它为准，不要再用番号寻址
- `status`：资源状态，取值见下表
- `is_fresh`：是否算新片（`release_date` 在 90 天内，含未来日期）。新片每轮都查、**不计次数、
  永不放弃**，所以它为 `true` 时 `attempt_count` 恒为 `0`，前端该展示「持续查询中」而不是次数
- `attempt_count` / `attempt_limit`：老片本轮没找到资源的次数与放弃阈值（默认 3）。
  成功找到资源后计数清零，所以下载中 / 已入库等状态恒为 0
- `dead_download_task_count`：该影片试过并判死的种子数
- `last_error`：仅 `status=failed` 时有值，为索引器调用的错误详情

### 状态取值

七项由服务端**一个** SQL CASE 表达式判定（`MovieSubscriptionService._status_expression()`），
筛选、计数、列表展示共用它。因此各状态严格互斥，`/status-counts` 各项之和恒等于 `total`。

⚠️ **`import_failed`（导入失败）与 `failed`（查询出错）是两回事**：前者是种子下完了、文件已经在
盘上，卡在入库那一步；后者是索引器调用出错、压根还没找到资源。前端文案必须分别念作「导入失败」
与「查询出错」，别都写成「失败」。

`downloading` 与 `import_failed` 是对「有活跃下载任务」这一集合的**二分**——两者并集恒等于该集合，
所以这次细分不改变任何影片的归属，只是把原来的一个桶一分为二。这条不变量必须守住：
资源查询的调度闸门（`SubscribedMovieAutoDownloadService._collect_due_movies` 里的
`~active_download_task_exists_expression()`）读的是同一个集合，两边脱节就会出现「页面说缺资源、
调度说别搜」的自相矛盾。**导入失败的影片同样不会被重新查资源**——文件已经在盘上，该修的是导入
而不是重下。

二分的切口是「导入还在不在途」（`import_status=pending/running`），不是「导入有没有报失败」。
因为导入终态不止 `failed`：整包只有小于阈值的样本文件时，扫描记 `skipped_count`、`failed_count`
为 0，任务会落成 `import_status=completed` 却一个 `Media` 都没产出。按 `failed` 切会把这类
「跑完了零产出」永远藏在「下载中」里。同理，一部片同时挂着一个导入失败的任务和一个还在下的任务时，
按「还在途」切才会正确显示「下载中」——还有希望，不该报导入失败。

「下完了正在等自动导入」仍归 `downloading`：那是秒级过渡态，用户对它的动作和真下载中一样（等着），
不值得再切一个状态。

| status | 含义 | 判定 |
|---|---|---|
| `imported` | 已入库 | 存在 `Media` |
| `downloading` | 下载中 | 无 `Media`，存在活跃 `DownloadTask` 且其中有 `import_status=pending/running` 的 |
| `import_failed` | 导入失败 | 无 `Media`，存在活跃 `DownloadTask` 但没有一个还在途（导入跑完了，库里没有） |
| `exhausted` | 已放弃 | 老片本轮没找到次数达到上限（默认 3），需手动重置 |
| `failed` | 查询出错 | 索引器调用失败，不计入本轮没找到次数，下轮重试 |
| `missing` | 缺资源 | 查过但没找到可用资源，下轮继续查 |
| `pending` | 待查 | 从未查过资源 |

表中顺序即 CASE 分支顺序，也就是优先级。`all` 仅用于查询入参，表示不按状态过滤。

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/movie-subscriptions` | 分页查询订阅影片及资源查询状态 |
| `GET` | `/movie-subscriptions/status-counts` | 各状态计数，供前端 tab 角标 |

资源查询重置已并入统一 action 协议（`POST /system/resource-task-actions`），本域不再有
重置端点，见下文。

采用顶层资源 `/movie-subscriptions` 而非挂在 `/movies` 下，是为了避免与
`/movies/{movie_number}` 抢路由匹配，也就不必依赖 router 的注册顺序。

## 详细接口定义

### `GET /movie-subscriptions`

分页查询订阅影片。筛选、排序、分页全部在 SQL 侧完成。

Query：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `page` | int | 1 | 页码 |
| `page_size` | int | 20 | 每页条数 |
| `status` | enum | `all` | 见状态取值表 |
| `sort` | enum | `subscribed_at:desc` | 见下 |
| `search` | string | – | 按番号 / 标题 / 中文标题模糊匹配 |

`sort` 可选值：`subscribed_at:desc|asc`、`release_date:desc|asc`、`last_searched_at:desc|asc`、
`attempt_count:desc`。空值统一排到最后。

响应：`PageResponse<MovieSubscriptionListItemResource>`。

`import_failed` 档的 `import_operation` 除作业 id、计数与 `available_actions` 外，
还返回首条失败条目的 `failure_reason`（原始码，如 `no_media_files_found`）与
`failure_detail`（如"下载目录中没有扫描到可导入的视频"），供订阅行直接展示
"为什么没导进去"；没有失败条目时两者为 `null`。详细 `failed_files`（含每条路径）
仍在导入作业详情接口 `GET /media-imports/import-jobs/{import_job_id}` 返回。

`import_operation.download_task_id` 是该失败导入关联的下载任务 id；存在时
`available_actions` 额外下发 `delete_failed_download`——订阅行据此复用下载中心的
删除任务语义：删除后影片不再有活跃下载任务，状态回到「缺资源」，下一轮自动下载
cron 会重新找种（内容闸门会避开原盘类候选）。

### `GET /movie-subscriptions/status-counts`

```json
{
  "total": 1200,
  "imported": 1050,
  "downloading": 9,
  "import_failed": 3,
  "pending": 30,
  "missing": 95,
  "exhausted": 12,
  "failed": 1
}
```

一次 `GROUP BY` 算齐全部计数，前端不需要为每个 tab 各打一次 COUNT。

### 资源查询重置（已并入统一 action 协议）

`POST /movie-subscriptions/search-resets` 已删除。对等调用
（协议详见[任务中心文档](../system/task-runs.md)）：

```json
POST /system/resource-task-actions
{
  "task_key": "subscribed_movie_auto_download",
  "action": "reset_retry_budget",
  "resource_ids": [movie_id]
}
```

- "重置全部已放弃"：缺省 `resource_ids`、带 `"state": "exhausted"`，后端按状态圈定整批
- 寻址从番号改为 `movie_id`（订阅列表项自带）；未订阅影片由合格性钩子跳过
  （`movie_not_subscribed`）
- 重置语义 = 重开预算（`retry_round + 1`、投影回 `pending`），尝试历史保留在 attempt 表，
  下轮定时任务即会重新查

**重置不放开选种黑名单。** `info_hash` 是内容寻址的——同一个 hash 就是同一个 swarm，换个索引器它
照样是死的；用户重置后真正想要的是让这部影片去找一个**别的**种子，而黑名单本来就不挡这个。确实要
重试某个具体种子时，手动删除该下载任务（UI 删任务会同步删 qB 侧与本地行）；`_prune_ghost_tasks`
对死态行豁免，仅凭在 qB 里删掉种子不会解除黑名单。

## 错误码

| code | HTTP | 说明 |
|---|---|---|
| `invalid_movie_subscription_filter` | 422 | 分页参数非法 |
