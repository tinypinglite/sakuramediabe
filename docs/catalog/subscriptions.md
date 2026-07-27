# Movie Subscriptions

## 资源说明

影片订阅管理：查看所有已订阅影片及其资源查询进展，以及手动重置资源查询状态让放弃的影片回到队列。

**订阅是长期意图标记**：影片入库之后订阅照常保留，不会自动解除，因此订阅列表只增不减。
前端默认视图应当是「需要关注的」（`missing` / `exhausted` / `failed`），全部订阅放次要位置。

订阅的写入侧（订阅 / 取消订阅）在 [movies.md](./movies.md)：
`PUT|DELETE /movies/{movie_number}/subscription`、`POST /movies/subscriptions`、
`POST /movies/unsubscriptions`。**批量取消订阅就用那边的 `POST /movies/unsubscriptions`**，本域
不另造一套；要连本地媒体文件一起删的走 `DELETE /media/{media_id}`。

资源查询本身的行为（查询次数与放弃、死种判定、选种黑名单）见
[transfers/downloads.md](../transfers/downloads.md) 的「内部定时任务」。

## 资源模型

```json
{
  "movie_number": "ABP-123",
  "title": "…",
  "title_zh": "…",
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

- `status`：资源状态，取值见下表
- `is_fresh`：是否算新片（`release_date` 在 90 天内，含未来日期）。新片每轮都查、**不计次数、
  永不放弃**，所以它为 `true` 时 `attempt_count` 恒为 `0`，前端该展示「持续查询中」而不是次数
- `attempt_count` / `attempt_limit`：老片已查次数与上限（默认 3）
- `dead_download_task_count`：该影片试过并判死的种子数
- `last_error`：仅 `status=failed` 时有值，为索引器调用的错误详情

### 状态取值

六项由服务端**一个** SQL CASE 表达式判定（`MovieSubscriptionService._status_expression()`），
筛选、计数、列表展示共用它。因此各状态严格互斥，`/status-counts` 各项之和恒等于 `total`。

| status | 含义 | 判定 |
|---|---|---|
| `imported` | 已入库 | 存在 `Media` |
| `downloading` | 下载中 | 无 `Media`，存在活跃 `DownloadTask` |
| `exhausted` | 已放弃 | 老片查询次数用尽，需手动重置 |
| `failed` | 查询出错 | 索引器调用失败，不消耗次数，下轮重试 |
| `missing` | 缺资源 | 查过但没找到可用资源，下轮继续查 |
| `pending` | 待查 | 从未查过资源 |

表中顺序即 CASE 分支顺序，也就是优先级。`all` 仅用于查询入参，表示不按状态过滤。

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/movie-subscriptions` | 分页查询订阅影片及资源查询状态 |
| `GET` | `/movie-subscriptions/status-counts` | 各状态计数，供前端 tab 角标 |
| `POST` | `/movie-subscriptions/search-resets` | 重置资源查询状态 |

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

### `GET /movie-subscriptions/status-counts`

```json
{
  "total": 1200,
  "imported": 1050,
  "downloading": 12,
  "pending": 30,
  "missing": 95,
  "exhausted": 12,
  "failed": 1
}
```

一次 `GROUP BY` 算齐全部计数，前端不需要为每个 tab 各打一次 COUNT。

### `POST /movie-subscriptions/search-resets`

把之前放弃的影片放回查询队列，下轮定时任务即会重新查。

请求：

```json
{ "movie_numbers": ["ABP-123"], "reset_all_exhausted": false }
```

- `reset_all_exhausted=true` 时忽略 `movie_numbers`，重置全部 `exhausted` 的订阅影片
- `reset_all_exhausted=false` 且 `movie_numbers` 为空 → `422 invalid_movie_subscription_reset`

响应：

```json
{ "reset_count": 12 }
```

`reset_count` 是被删掉的状态行数。

重置动作就是**删掉这些影片的 `ResourceTaskState` 行**，让它们回到「从未查过」。没有状态行本来就是
合法的初始态（调度器与本页都按 LEFT JOIN 处理空行），所以不需要额外维护一套「重置后各字段该长
什么样」的规则。

**重置不放开选种黑名单。** `info_hash` 是内容寻址的——同一个 hash 就是同一个 swarm，换个索引器它
照样是死的；用户重置后真正想要的是让这部影片去找一个**别的**种子，而黑名单本来就不挡这个。确实要
重试某个具体种子时，从 qB 里删掉它即可，`DownloadSyncService._prune_ghost_tasks` 的反向对账会同步
删掉本地台账行，该 hash 随之离开黑名单。

## 错误码

| code | HTTP | 说明 |
|---|---|---|
| `invalid_movie_subscription_filter` | 422 | 分页参数非法 |
| `invalid_movie_subscription_reset` | 422 | 既没给 `movie_numbers` 也没开 `reset_all_exhausted` |
