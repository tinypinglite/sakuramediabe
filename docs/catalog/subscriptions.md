# 影片订阅

订阅是长期意图标记：影片入库后仍保留。订阅和取消订阅使用[影片接口](./movies.md)；本域提供订阅影片的管理视图。

## 状态

订阅页把媒体、下载事实和影片自身的搜索状态合并为七个互斥状态：

| status | 含义 |
|---|---|
| `imported` | 已有媒体入库 |
| `downloading` | 有下载或导入仍在进行 |
| `import_failed` | 下载已结束但没有媒体入库 |
| `exhausted` | 老片连续未找到资源达到上限，等待重开 |
| `failed` | 索引器或提交链路出错，下轮会重试 |
| `missing` | 查过但没有可用资源，下轮会继续搜索 |
| `pending` | 订阅后尚未搜索 |

`imported`、`downloading`、`import_failed` 优先于搜索状态。列表、筛选和状态计数共用同一 SQL 判定，计数之和等于订阅总数。

每项还返回 `is_fresh`、`attempt_count`、`attempt_limit`、`last_searched_at` 和 `last_error`。新片不消耗未找到预算；老片达到上限后变为 `exhausted`。

## 端点

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/movie-subscriptions` | 分页查询订阅影片 |
| `GET` | `/movie-subscriptions/status-counts` | 查询各状态计数 |
| `POST` | `/movie-subscriptions/search-resets` | 重开搜索预算 |

`POST /movie-subscriptions/search-resets` 省略请求体时重开全部 `exhausted` 订阅；传入 `{"movie_ids": [123, 456]}` 时仅重开指定影片。响应为 `{"reset_count": 2}`。
