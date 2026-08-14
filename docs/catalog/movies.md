# 影片 API（按当前代码实现）

## 资源说明

当前电影路由实现以下能力：

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

- `POST /movies/search/parse-number`：解析用户输入，提取影片番号
- `GET /movies/search/local`：本地库按番号精确搜索
- `GET /movies/{movie_number}/collection-status`：读取本地影片是否合集
- `PATCH /movies/collection-type`：批量标记影片为合集或单体
- `GET /movies/{movie_number}/reviews`：按影片番号读取 JavDB 评论
- `GET /movies/{movie_number}/subtitles`：按影片番号读取字幕列表
- `GET /movies/{movie_number}/similar`：读取离线预计算的相似影片列表
- `POST /movies/search/javdb/stream`：JavDB 按番号搜索并流式入库
- `POST /movies/series/{series_id}/javdb/import/stream`：按本地系列 ID 抓取 JavDB 系列影片并流式入库
- `POST /movies/{movie_number}/metadata-refresh`：严格刷新本地已有影片的远端元数据
- `POST /movies/{movie_number}/heat-recompute`：手动重算单部影片热度

> 单片互动同步端点已删除：统一走 `POST /system/resource-task-actions` 的
> `rerun`（`resource_ids=[movie_id]`，202 入队语义），见
> [任务中心文档](../system/task-runs.md)。
- `GET /movies`：分页查询影片列表
- `GET /movies/latest`：分页查询最新入库影片
- `GET /movies/subscribed-actors/latest`：分页查询已订阅演员的最新影片
- `POST /movies/by-series`：按本地系列 ID 查询同一系列下的影片
- `GET /movies/{movie_number}`：查询影片详情

关键点：

- 影片主标识是 `movie_number`（路径参数）
- 搜索接口只接受 `movie_number` 参数
- 番号搜索使用“标准化后精确匹配”（兼容空白、大小写、`PPV-` 前缀）
- 列表筛选支持 `actor_id`、`status`、`collection_type`、`sort`，以及特殊标签筛选 `special_tag`
- 文档字段统一使用 `snake_case`

## 资源模型

通用图片结构见 [images.md](./images.md)。

影片摘要（`MovieListItemResource`）：

```json
{
  "id": 1,
  "javdb_id": "MovieA1",
  "movie_number": "ABC-001",
  "title": "Movie 1",
  "series_id": 1,
  "series_name": "Series 1",
  "cover_image": {
    "id": 10,
    "origin": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>"
  },
  "thin_cover_image": {
    "id": 11,
    "origin": "/files/images/movies/ABC-001/thin-cover.jpg?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/thin-cover.jpg?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/thin-cover.jpg?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/thin-cover.jpg?expires=1700000900&signature=<signature>"
  },
  "release_date": "2024-01-02",
  "duration_minutes": 120,
  "score": 4.5,
  "watched_count": 12,
  "want_watch_count": 23,
  "comment_count": 34,
  "score_number": 45,
  "heat": 0,
  "is_collection": true,
  "is_subscribed": false,
  "can_play": true,
  "is_4k": true
}
```

影片详情（`MovieDetailResource`）沿用摘要中的 `cover_image`、`thin_cover_image`，并额外增加：

- `actors`: `MovieActorResource[]`
- `tags`: `TagResource[]`
- `summary`: `string`（摘要；`desc` / `desc_zh` 存量数据已迁移至此，中文描述优先）
- `maker_name`: `string | null`（厂商名称）
- `director_name`: `string | null`（导演名称）
- `plot_images`: `ImageResource[]`
- `media_items`: `MovieMediaResource[]`
- `cover_image`、`thin_cover_image`、`plot_images` 中的图片字段都返回带签名的文件访问路径

其中：

- `id`: 影片主键（整数）。对外主标识仍是 `movie_number`，但统一资源任务操作
  （`POST /system/resource-task-actions` 的 `resource_ids`）收的是这个 id，列表与详情都会返回
- `series_name`: 系列名称，可为 `null`
- `series_id`: 系列 ID，可为 `null`；系列名来自独立 `movie_series` 表
- `title`：标题；翻译链路下线后，存量 `title_zh`（中文标题）已合并进本字段
- `thin_cover_image`：优先由封面图裁切生成；若裁切失败，则回退到前两张剧情图中的第一张竖图；若仍未命中则为 `null`
- `heat`: 影片热度值，整数且非空；默认 `0`
- `score`、`score_number`、`watched_count`、`want_watch_count`、`comment_count` 会由定时互动同步任务定期从 JavDB 回刷
- `maker_name`、`director_name` 仅在详情接口返回，列表接口不返回这些字段

影片热度使用累计关注度公式（当前版本 `v6`）：

```text
W = watched_count / 1308
I = want_watch_count / 4991
C = comment_count / 41
R = score_number / 6291

heat = ROUND(3100 × (7/34 × W + 5/34 × I + 17/34 × C + 5/34 × R))
```

参考值固定为当前业务库互动数据的 P99，不随每日全库重算动态变化；`3100` 只是 P99 附近的展示基准，不是上限，P99 以上的影片继续按原始计数线性增长。评论数占 50%，`score` 本身表示平均评分，不参与关注度计算。

`MovieMediaResource`：

- `media_id`: 媒体 ID
- `library_id`: 媒体库 ID（可空）
- `play_url`: 媒体播放地址；返回带签名的相对 URL，可直接与 `base_url` 拼接访问
- `storage_mode`: 媒体存储模式（可空）
- `resolution`: 分辨率（可空）
- `file_size_bytes`: 文件大小（字节）
- `duration_seconds`: 时长（秒）
- `special_tags`: 特殊标签；其中本地媒体的 `4K` 来自真实视频流解析，不再按文件名、`.iso` 或体积推断
- `valid`: 媒体有效性
- `progress`: `MovieMediaProgressResource | null`
- `points`: `MovieMediaPointResource[]`
  每个点位包含 `point_id`、`thumbnail_id`、`offset_seconds` 与 `image`（签名图片路径）
- `subtitles`: 不再内嵌在 `media_items` 中，统一通过 `GET /movies/{movie_number}/subtitles` 查询
- `is_4k`: 影片聚合字段；只要存在任意一条 `valid=true` 且特殊标签包含 `4K` 的本地媒体，就返回 `true`

`MovieSubtitleListResource`：

- `movie_number`: 影片番号
- `items`: `MovieSubtitleItemResource[]`
  每个条目包含 `subtitle_id`、`file_name`、`created_at`、`url`
  `url` 格式为 `/files/subtitles/{subtitle_id}?expires=...&signature=...`

相似影片摘要（`SimilarMovieListItemResource`）：

- 基于 `MovieListItemResource`
- 额外字段：
  - `similarity_score`: `float`，相似度分数，按降序返回
- 数据来源：
  - 离线任务把演员、标签构造成 IDF 加权稀疏向量并写入 Qdrant
  - 请求接口只查询已激活的 Qdrant 索引，不在请求线程中临时计算
  - 排序只取决于内容相似度，不再叠加影片热度加成
  - 首次索引尚未构建完成时返回 `503`（`movie_similarity_index_not_ready`）
  - Qdrant 不可用时降级返回空列表，并记录 warning 日志

`MovieActorResource`：

```json
{
  "id": 1,
  "javdb_id": "ActorA1",
  "name": "三上悠亚",
  "alias_name": "三上悠亚 / 鬼头桃菜",
  "is_subscribed": false,
  "profile_image": null
}
```

`ImageResource` 当前典型路径：

- 封面：`/files/images/movies/{movie_number}/cover.jpg?...`
- 剧照：`/files/images/movies/{movie_number}/plots/{index}.jpg?...`
- 演员头像：`/files/images/actors/{javdb_id}.jpg?...`

分页响应：

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0
}
```

## 接口总览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/movies/search/parse-number` | 解析输入并提取影片番号 |
| `GET` | `/movies/search/local` | 本地库按番号搜索（0~1 条） |
| `GET` | `/movies/{movie_number}/collection-status` | 查询本地影片是否合集 |
| `PATCH` | `/movies/collection-type` | 批量标记影片为合集或单体 |
| `GET` | `/movies/{movie_number}/reviews` | 读取影片评论（按本地影片映射到 javdb_id） |
| `GET` | `/movies/{movie_number}/subtitles` | 查询影片字幕列表 |
| `GET` | `/movies/{movie_number}/similar` | 查询相似影片列表 |
| `POST` | `/movies/search/javdb/stream` | JavDB 按番号搜索并流式入库（SSE） |
| `POST` | `/movies/series/{series_id}/javdb/import/stream` | JavDB 按本地系列 ID 抓取系列影片并流式入库（SSE） |
| `POST` | `/movies/{movie_number}/metadata-refresh` | 严格刷新本地已有影片的远端元数据 |
| `POST` | `/movies/{movie_number}/heat-recompute` | 手动重算单部影片热度 |
| `GET` | `/movies` | 分页查询影片 |
| `GET` | `/movies/latest` | 分页查询最新入库影片 |
| `GET` | `/movies/subscribed-actors/latest` | 分页查询已订阅演员的最新影片 |
| `POST` | `/movies/by-series` | 按本地系列 ID 分页查询影片 |
| `PUT` | `/movies/{movie_number}/subscription` | 订阅影片（单条） |
| `DELETE` | `/movies/{movie_number}/subscription` | 取消订阅影片 |
| `POST` | `/movies/subscriptions` | 批量订阅影片（部分成功） |
| `POST` | `/movies/unsubscriptions` | 批量取消订阅影片（部分成功） |
| `GET` | `/movies/{movie_number}` | 查询影片详情 |

> 订阅的**管理视图**（订阅列表、资源查询状态与次数、缺失影片、重置查询状态）在独立的顶层资源
> `/movie-subscriptions`，见 [subscriptions.md](./subscriptions.md)。本文档只覆盖订阅状态的写入侧。
> 批量取消订阅就是上表的 `POST /movies/unsubscriptions`，管理页也调它——订阅管理域不另造一套；
> 要连媒体文件一起删的走 `DELETE /media/{media_id}`。
>
> 写入侧的一条联动：影片从「未订阅」变为「订阅」时，会顺带重置该影片的资源查询状态行
> （`ResourceTaskState`，`task_key=subscribed_movie_auto_download`；重置 = 重开预算而非删行，
> 尝试历史保留在 `resource_task_attempt`）。取消订阅不重置，不重置的话曾被判 `exhausted`
> 的影片重新订阅后会一直被自动下载跳过。

## 详细接口定义

### `POST /movies/search/parse-number`

- 鉴权：需要 Bearer Token
- 请求体：
  - `query`：用户输入（必填）
- 行为：
  - 解析成功：返回 `parsed=true` 和 `movie_number`
  - 解析失败：仍返回 `200`，`parsed=false`，`reason=movie_number_not_found`

示例请求：

```http
POST /movies/search/parse-number
Authorization: Bearer <token>
Content-Type: application/json

{
  "query": "path/to/abp123.mp4"
}
```

解析成功响应：

```json
{
  "query": "path/to/abp123.mp4",
  "parsed": true,
  "movie_number": "ABP-123",
  "reason": null
}
```

解析失败响应：

```json
{
  "query": "hello world",
  "parsed": false,
  "movie_number": null,
  "reason": "movie_number_not_found"
}
```

### `GET /movies/search/local`

- 鉴权：需要 Bearer Token
- Query：
  - `movie_number`：影片番号（必填，最小长度 1）
- 行为：
  - 仅按番号精确匹配，返回 `MovieListItemResource[]`（数量 `0~1`）
  - 匹配前会标准化（去空白、统一大写、兼容 `PPV-`）

示例请求：

```http
GET /movies/search/local?movie_number=fc2-123456
Authorization: Bearer <token>
```

示例响应：

```json
[
  {
    "id": 1,
    "javdb_id": "MovieA1",
    "movie_number": "FC2-PPV-123456",
    "title": "Movie 1",
    "series_name": null,
    "cover_image": null,
    "thin_cover_image": null,
    "release_date": null,
    "duration_minutes": 0,
    "score": 0.0,
    "watched_count": 0,
    "want_watch_count": 0,
    "comment_count": 0,
    "score_number": 0,
    "heat": 0,
    "is_collection": false,
    "is_subscribed": false,
    "can_play": false,
    "is_4k": false
  }
]
```

### `GET /movies/{movie_number}/similar`

- 鉴权：需要 Bearer Token
- Query：
  - `limit`：返回条数，默认 `20`，范围 `0~100`
- 行为：
  - 按标准化后的影片番号定位 source 影片（兼容空白、大小写、`PPV-` 前缀）
  - 查询 Qdrant 稀疏向量索引，按相似度降序返回
  - 响应项复用 `MovieListItemResource`，并附加 `similarity_score`
  - 影片尚未进入索引（无演员无标签，或建索引后新入库）时返回空列表

示例请求：

```http
GET /movies/FC2-123456/similar?limit=5
Authorization: Bearer <token>
```

示例响应：

```json
[
  {
    "id": 2,
    "javdb_id": "MovieA2",
    "movie_number": "FC2-PPV-654321",
    "title": "Movie 2",
    "series_name": null,
    "cover_image": null,
    "thin_cover_image": null,
    "release_date": null,
    "duration_minutes": 0,
    "score": 0.0,
    "watched_count": 0,
    "want_watch_count": 0,
    "comment_count": 0,
    "score_number": 0,
    "heat": 0,
    "is_collection": false,
    "is_subscribed": false,
    "can_play": true,
    "is_4k": true,
    "similarity_score": 0.91
  }
]
```

### `PATCH /movies/collection-type`

- 鉴权：需要 Bearer Token
- 请求体：
  - `movie_numbers`：影片番号数组（至少 1 个，单项不能为空）
  - `collection_type`：目标类型（`collection | single`）
- 行为：
  - 输入番号按标准化规则匹配（去空白、统一大写、兼容 `PPV-`）
  - 未匹配到本地影片的番号会静默忽略
  - 命中的影片会批量更新：
    - `collection` -> `is_collection=true`
    - `single` -> `is_collection=false`
  - 同时写入手动覆盖标记，后续自动规则同步和导入流程不会改写该影片的合集状态
- 成功响应：
  - `200 OK`
  - 返回 `requested_count`（请求内原始番号数量）和 `updated_count`（命中并写入数量）

示例请求：

```http
PATCH /movies/collection-type
Authorization: Bearer <token>
Content-Type: application/json

{
  "movie_numbers": ["fc2-123456", "ABP-123", "ABP-404"],
  "collection_type": "single"
}
```

示例响应：

```json
{
  "requested_count": 3,
  "updated_count": 2
}
```

### `POST /movies/{movie_number}/metadata-refresh`

- 鉴权：需要 Bearer Token
- 路径参数：
  - `movie_number`：影片番号
- 行为：
  - 仅刷新本地已存在影片；若本地不存在，返回 `404 movie_not_found`
  - 路径参数会按现有标准化规则匹配本地影片，兼容空白、大小写与 `PPV-` 差异
  - 会严格刷新以下远端元数据：
    - JavDB ID、标题、封面、上映日期、时长、评分/人数、摘要、系列、厂商、导演、额外元数据
    - 演员、标签、剧情图关系按远端当前结果全量重建，旧关联会删除
    - 封面、剧情图、当前演员列表中的演员头像会强制重下，不复用旧文件
    - `thin_cover_image` 会基于最新封面和剧情图重新计算：优先裁切封面，失败时回退到前两张剧情图中的第一张竖图；仍未命中则清空
  - 不会刷新：
    - `movie_number`
    - 订阅状态、合集状态、热度等本地状态字段
  - 远端查不到番号时返回 `404 movie_metadata_not_found`
  - 远端请求失败、图片刷新失败或刷新过程异常时返回 `502 movie_metadata_refresh_failed`
  - 若远端返回的番号标准化后与本地影片不一致，返回 `409 movie_metadata_number_conflict`
  - 若远端返回的 `javdb_id` 已被其他本地影片占用，返回 `409 movie_metadata_javdb_id_conflict`
- 成功响应：
  - 返回最新 `MovieDetailResource`

示例请求：

```http
POST /movies/abp123/metadata-refresh
Authorization: Bearer <token>
```

示例响应：

```json
{
  "id": 1,
  "javdb_id": "MovieA1",
  "movie_number": "ABP-123",
  "title": "Movie 1",
  "series_id": 1,
  "series_name": "Series 1",
  "cover_image": {
    "id": 10,
    "origin": "/files/images/movies/ABP-123/cover.jpg?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABP-123/cover.jpg?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABP-123/cover.jpg?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABP-123/cover.jpg?expires=1700000900&signature=<signature>"
  },
  "release_date": "2024-01-02",
  "duration_minutes": 120,
  "score": 4.5,
  "watched_count": 12,
  "want_watch_count": 23,
  "comment_count": 34,
  "score_number": 45,
  "heat": 0,
  "is_collection": false,
  "is_subscribed": false,
  "can_play": true,
  "is_4k": true,
  "actors": [],
  "tags": [],
  "summary": "summary",
  "maker_name": "maker",
  "director_name": "director",
  "thin_cover_image": null,
  "plot_images": [],
  "media_items": [],
  "playlists": []
}
```

### 单片互动同步（已并入统一 action 协议）

`POST /movies/{movie_number}/interaction-sync` 已删除（影片简介翻译链路整体下线）。对等调用：

```json
POST /system/resource-task-actions
{
  "task_key": "movie_interaction_sync",
  "action": "rerun",
  "resource_ids": [movie_id]
}
```

- `resource_ids` 收整数影片主键，取影片摘要 / 详情响应里的 `id` 字段（不是 `movie_number`，
  也不是 `javdb_id`）
- `rerun` 是强制语义：互动同步不受批量调度刷新窗口限制
- 202 入队语义：执行在 worker，响应携带 `task_run_id`，前端经 SSE / 单条查询跟进后
  刷新影片详情
- 影片缺 JavDB ID 由合格性钩子逐条跳过（`movie_javdb_id_missing`），不再返回 422

### `POST /movies/{movie_number}/heat-recompute`

- 鉴权：需要 Bearer Token
- 路径参数：
  - `movie_number`：影片番号
- 行为：
  - 按与 `metadata-refresh` 相同的标准化规则匹配本地影片
  - 仅按当前热度公式重算这部影片的 `heat`
  - 成功后返回最新 `MovieDetailResource`
- 错误：
  - `404 movie_not_found`
  - `500 movie_heat_recompute_failed`

### `GET /movies/{movie_number}/collection-status`

- 鉴权：需要 Bearer Token
- 路径参数：
  - `movie_number`：影片番号（必填）
- 行为：
  - 仅按本地 `Movie.is_collection` 返回合集状态，不做配置规则兜底推断
  - 匹配前会标准化（去空白、统一大写、兼容 `PPV-`）
  - 命中返回库内标准化后的 `movie_number` 与 `is_collection`
  - 未命中返回 `404 movie_not_found`

示例请求：

```http
GET /movies/fc2-123456/collection-status
Authorization: Bearer <token>
```

示例响应：

```json
{
  "movie_number": "FC2-PPV-123456",
  "is_collection": true
}
```

未命中响应：

```json
{
  "error": {
    "code": "movie_not_found",
    "message": "影片不存在",
    "details": {
      "movie_number": "ABP-404"
    }
  }
}
```

### `POST /movies/search/javdb/stream`

- 鉴权：需要 Bearer Token
- 请求体：
  - `movie_number`：影片番号（必填）
- 响应：
  - `200 OK`
  - `Content-Type: text/event-stream`
  - 事件顺序与演员流式接口一致，最终结果看 `completed`
- 导入语义：**纯新建**——影片已存在时跳过不更新任何字段（`already_exists_count` 计数），
  需要按 JavDB 全量刷新已存在影片请用 `POST /movies/{movie_number}/metadata-refresh`。
- 事件顺序：
  - `search_started`
  - `movie_found`
  - `upsert_started`
  - `upsert_finished`
  - `completed`

示例请求：

```http
POST /movies/search/javdb/stream
Authorization: Bearer <token>
Content-Type: application/json

{
  "movie_number": "ABP-123"
}
```

成功事件流示例：

```text
event: search_started
data: {"movie_number":"ABP-123"}

event: movie_found
data: {"movies":[{"javdb_id":"javdb-ABP-123","movie_number":"ABP-123","title":"title-ABP-123","cover_image":"https://example.com/cover.jpg"}],"total":1}

event: upsert_started
data: {"total":1}

event: upsert_finished
data: {"total":1,"created_count":1,"already_exists_count":0,"failed_count":0}

event: completed
data: {"success":true,"movies":[{"javdb_id":"javdb-ABP-123","movie_number":"ABP-123","title":"title-ABP-123","cover_image":null,"thin_cover_image":null,"release_date":null,"duration_minutes":0,"score":0.0,"watched_count":0,"want_watch_count":0,"comment_count":0,"score_number":0,"is_collection":false,"is_subscribed":false}],"failed_items":[],"stats":{"total":1,"created_count":1,"already_exists_count":0,"failed_count":0}}
```

未找到事件流示例：

```text
event: search_started
data: {"movie_number":"ABP-404"}

event: completed
data: {"success":false,"reason":"movie_not_found","movies":[]}
```

### `POST /movies/series/{series_id}/javdb/import/stream`

- 鉴权：需要 Bearer Token
- 路径参数：
  - `series_id`：本地 `movie_series.id`，不是 JavDB 系列 ID
- 请求体：无
- 响应：
  - `200 OK`
  - `Content-Type: text/event-stream`
- 行为：
  - 先读取本地系列名称，再用该名称搜索 JavDB 系列
  - 只接受 JavDB 系列名与本地系列名 `strip` 后完全一致的候选
  - 多个精确同名候选时使用第一个
  - JavDB 系列影片列表里的本地已有影片直接跳过，不刷新元数据
  - 不存在的影片会先按 `javdb_id` 拉详情，再复用目录导入服务入库
- 事件顺序：
  - `search_started`
  - `series_found`
  - `javdb_series_found`
  - `movie_found`
  - `upsert_started`
  - 若干 `movie_skipped` / `movie_upsert_started` / `movie_upsert_finished`
  - `upsert_finished`
  - `completed`
- 失败原因：
  - `local_series_not_found`：本地系列不存在
  - `javdb_series_not_found`：JavDB 搜索不到精确同名系列
  - `javdb_series_movies_not_found`：JavDB 系列没有影片
  - `metadata_fetch_failed`：搜索系列或获取系列影片列表失败
  - `internal_error`：所有待导入影片均失败或出现未预期异常

示例请求：

```http
POST /movies/series/12/javdb/import/stream
Authorization: Bearer <token>
```

成功事件流示例：

```text
event: search_started
data: {"series_id":12}

event: series_found
data: {"series_id":12,"series_name":"S1 NO.1 STYLE"}

event: javdb_series_found
data: {"javdb_id":"series-1","javdb_type":0,"name":"S1 NO.1 STYLE","videos_count":2}

event: movie_found
data: {"movies":[{"javdb_id":"javdb-existing","movie_number":"ABP-001","title":"Existing","cover_image":null},{"javdb_id":"javdb-new","movie_number":"ABP-002","title":"New","cover_image":null}],"total":2}

event: upsert_started
data: {"total":2}

event: movie_skipped
data: {"javdb_id":"javdb-existing","movie_number":"ABP-001","reason":"already_exists","index":1,"total":2}

event: movie_upsert_started
data: {"javdb_id":"javdb-new","movie_number":"ABP-002","index":2,"total":2}

event: movie_upsert_finished
data: {"javdb_id":"javdb-new","movie_number":"ABP-002","index":2,"total":2}

event: upsert_finished
data: {"total":2,"created_count":1,"already_exists_count":1,"failed_count":0}

event: completed
data: {"success":true,"movies":[{"javdb_id":"javdb-new","movie_number":"ABP-002","title":"New","cover_image":null,"thin_cover_image":null,"release_date":null,"duration_minutes":0,"score":0.0,"watched_count":0,"want_watch_count":0,"comment_count":0,"score_number":0,"is_collection":false,"is_subscribed":false}],"skipped_items":[{"javdb_id":"javdb-existing","movie_number":"ABP-001","reason":"already_exists"}],"failed_items":[],"stats":{"total":2,"created_count":1,"already_exists_count":1,"failed_count":0}}
```

未找到本地系列事件流示例：

```text
event: search_started
data: {"series_id":999}

event: completed
data: {"success":false,"reason":"local_series_not_found","movies":[]}
```

### `GET /movies`

- 鉴权：需要 Bearer Token
- Query：
  - `actor_id`：按演员 ID 过滤（可选）
  - `tag_ids`：按标签 ID 列表过滤（可选，逗号分隔，如 `1,2,3`），与 `tag_match` 配合决定组合方式
  - `tag_match`：多个标签的组合关系（可选，`or | and`，默认 `or`）；`or` 命中任意标签即返回，`and` 须同时包含全部标签，仅在传 `tag_ids` 时生效
  - `director_name`：按导演名称精确过滤（可选；会先 `strip`）
  - `maker_name`：按厂商名称精确过滤（可选；会先 `strip`）
  - `year`：按发行年份过滤（可选，只支持单个年份）
  - `status`：按影片状态过滤（可选，`all | subscribed | unsubscribed | playable`，默认 `all`）
  - `collection_type`：按合集类型过滤（可选，`all | single`，默认 `all`；`single` 表示 `is_collection=false`）
  - `special_tag`：按特殊标签过滤（可选，`4k | uncensored | vr`）
  - `heat_min`：热度下限（可选，整数且 `>= 0`，闭区间）；与 `heat_max` 配合实现热度范围过滤，如 `heat_min=40&heat_max=80`；仅传下限时表示热度无上界，未同步热度（`heat=0`）的影片会被排除
  - `heat_max`：热度上限（可选，整数且 `>= 0`，闭区间）；仅传上限时会把未同步热度（`heat=0`）的影片一并包含，需要明确下界时请配合 `heat_min`
  - `sort`：排序表达式（可选，格式 `field:direction`）
    - `field` 支持：`release_date`、`added_at`、`subscribed_at`、`comment_count`、`score_number`、`want_watch_count`、`heat`
    - `direction` 支持：`asc | desc`
  - `page`：默认 `1`
  - `page_size`：默认 `20`
- 行为：
  - 未传 `sort` 时，按 `movie.movie_number` 升序
  - 传入 `sort` 时，按指定字段和方向排序；若主排序值相同，则按 `movie.id` 同方向稳定排序
  - `sort=added_at:*` 在 `status=playable` 时按每部影片关联媒体的 `MAX(media.created_at)` 排序；其他状态仍按 `movie.id` 表示的影片记录插入顺序排序
  - `release_date`、`subscribed_at` 为空的影片始终排在最后
  - `total` 为过滤后的影片总数
  - `tag_ids` 默认（`tag_match=or`）只返回至少命中一个指定标签的影片；`tag_match=and` 只返回同时包含全部指定标签的影片
  - `director_name`、`maker_name` 均为精确匹配，空白值返回 422 `invalid_movie_filter`
  - `year` 只返回 `release_date` 落在该自然年的影片
  - `status=subscribed` 只返回已订阅影片
  - `status=unsubscribed` 只返回未订阅影片
  - `status=playable` 只返回存在有效媒体的影片
  - `collection_type=single` 只返回 `is_collection=false` 的影片
  - `special_tag=4k` 只返回存在有效 `4K` 媒体的影片；`uncensored`、`vr` 同理
  - `heat_min` / `heat_max` 只返回热度落在 `[heat_min, heat_max]` 闭区间内的影片；`heat_min > heat_max` 返回 422 `invalid_movie_filter`

示例请求：

```http
GET /movies?actor_id=1&page=1&page_size=20
```

```http
GET /movies?status=subscribed&page=1&page_size=20
```

```http
GET /movies?actor_id=1&status=playable&page=1&page_size=20
```

```http
GET /movies?status=unsubscribed&heat_min=40&heat_max=80&sort=heat:desc&page=1&page_size=20
```

```http
GET /movies?tag_ids=1,2&year=2024&page=1&page_size=20
```

```http
GET /movies?tag_ids=1,2&tag_match=and&page=1&page_size=20
```

```http
GET /movies?director_name=嵐山みちる&maker_name=S1%20NO.1%20STYLE&page=1&page_size=20
```

```http
GET /movies?special_tag=4k&page=1&page_size=20
```

```http
GET /movies?collection_type=single&sort=release_date:desc&page=1&page_size=20
```

示例响应：

```json
{
  "items": [
    {
      "id": 1,
      "javdb_id": "MovieA1",
      "movie_number": "ABC-001",
      "title": "Movie 1",
      "series_id": null,
      "series_name": null,
      "cover_image": null,
      "thin_cover_image": null,
      "release_date": null,
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false,
      "is_4k": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### `GET /movies/latest`

- 鉴权：需要 Bearer Token
- Query：
  - `page`：默认 `1`
  - `page_size`：默认 `20`
- 行为：
  - 仅返回至少关联一条 `media` 记录的影片
  - 每部影片按其关联媒体中的 `MAX(media.created_at)` 作为“最新入库时间”
  - 按“最新入库时间”降序排序；若时间相同，则按 `movie.id` 降序稳定排序
  - `total` 为存在媒体的去重影片总数，不是媒体条数

示例请求：

```http
GET /movies/latest?page=1&page_size=20
Authorization: Bearer <token>
```

示例响应：

```json
{
  "items": [
    {
      "id": 2,
      "javdb_id": "MovieA2",
      "movie_number": "ABC-002",
      "title": "Movie 2",
      "series_name": null,
      "cover_image": null,
      "thin_cover_image": null,
      "release_date": null,
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": true,
      "is_4k": false
    },
    {
      "id": 1,
      "javdb_id": "MovieA1",
      "movie_number": "ABC-001",
      "title": "Movie 1",
      "series_id": null,
      "series_name": null,
      "cover_image": null,
      "thin_cover_image": null,
      "release_date": null,
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false,
      "is_4k": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 2
}
```

### `GET /movies/subscribed-actors/latest`

- 鉴权：需要 Bearer Token
- Query：
  - `page`：默认 `1`
  - `page_size`：默认 `20`
- 行为：
  - 仅返回至少关联一位已订阅演员（`actor.is_subscribed=true`）的影片
  - 默认排除合集影片（`movie.is_collection=true`）
  - 同一影片关联多位已订阅演员时只返回一条（去重）
  - 按 `movie.release_date` 降序排序；`release_date=null` 的影片排在最后
  - `release_date` 相同时，按 `movie.id` 降序稳定排序
  - `total` 为过滤后的去重影片总数
  - 与 `/movies/latest` 不同：本接口按上映日期排序，不要求影片存在本地媒体

示例请求：

```http
GET /movies/subscribed-actors/latest?page=1&page_size=20
Authorization: Bearer <token>
```

示例响应：

```json
{
  "items": [
    {
      "id": 2,
      "javdb_id": "MovieA2",
      "movie_number": "ABC-002",
      "title": "Movie 2",
      "series_name": null,
      "cover_image": null,
      "thin_cover_image": null,
      "release_date": "2026-03-10",
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": true,
      "is_4k": false
    },
    {
      "id": 1,
      "javdb_id": "MovieA1",
      "movie_number": "ABC-001",
      "title": "Movie 1",
      "series_id": null,
      "series_name": null,
      "cover_image": null,
      "thin_cover_image": null,
      "release_date": null,
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false,
      "is_4k": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 2
}
```

### `POST /movies/by-series`

- 鉴权：需要 Bearer Token
- 请求体：
  - `series_id`：本地 `movie_series.id`（必填，正整数；不是 JavDB 系列 ID）
  - `sort`：排序表达式（可选，详见 `GET /movies` 的 `sort`；非法取值返回 422 `invalid_movie_filter`）
  - `page`：默认 `1`
  - `page_size`：默认 `20`，范围 `[1, 100]`
- 行为：
  - 直接使用 `movie.series_id = series_id` 查询同一系列下的影片
  - 不存在的正整数 `series_id` 返回 `total=0`、`items=[]`
  - 响应结构与 `GET /movies` 一致，前端列表组件可直接复用

示例请求：

```http
POST /movies/by-series
Authorization: Bearer <token>
Content-Type: application/json

{
  "series_id": 12,
  "sort": "release_date:desc",
  "page": 1,
  "page_size": 20
}
```

示例响应：

```json
{
  "items": [
    {
      "id": 2,
      "javdb_id": "MovieA2",
      "movie_number": "ABP-121",
      "title": "Movie 2",
      "series_id": 12,
      "series_name": "S1 NO.1 STYLE",
      "cover_image": null,
      "thin_cover_image": null,
      "release_date": "2026-03-10",
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false,
      "is_4k": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### `GET /movies/{movie_number}/reviews`

- 鉴权：需要 Bearer Token
- Path：
  - `movie_number`：影片番号（字符串）
- Query：
  - `page`：默认 `1`，最小值 `1`
  - `page_size`：默认 `20`，最小值 `1`（映射到 JavDB 接口 `limit`）
  - `sort`：默认 `recently`，支持 `recently | hotly`
- 行为：
  - 先按本地 `movie_number` 查影片并读取 `javdb_id`
  - 再调用 JavDB 评论接口按 `javdb_id` 拉取评论
  - 返回 `JavdbMovieReviewResource[]`，不包装分页对象

示例请求：

```http
GET /movies/ABC-001/reviews?page=1&page_size=20&sort=recently
Authorization: Bearer <token>
```

示例响应：

```json
[
  {
    "id": 123,
    "score": 4,
    "content": "很不错",
    "created_at": "2026-03-10T08:00:00",
    "username": "tester",
    "like_count": 5,
    "watch_count": 10
  }
]
```

### `GET /movies/{movie_number}/subtitles`

- 鉴权：需要 Bearer Token
- Path：
  - `movie_number`：影片番号（字符串）
- 行为：
  - 先按 `movie_number` 查询影片
  - 返回当前可访问的字幕文件列表
  - 字幕下载地址使用 `subtitle_id` 签名协议，不再使用 `media_id + file_name`
  - 只读已存在的字幕记录，不再暴露后台抓取状态

存储位置：**新导入**的字幕统一落在 `<图片根>/movies/{shard}/{番号}/subtitles/`，本地导入与 115 导入共用
同一目录。新布局下字幕**跟番号走**而不跟具体 Media 文件走：媒体文件被删除或失效不会连带清掉字幕。

**向后兼容（迁移可选）**：老用户即使不跑 `migrate-movie-subtitles`，存量字幕也照常可读——运行时同时放行
新布局与两处老位置：115 旧字幕根 `<旧字幕根>/{番号}/`、以及媒体库里视频所在版本目录的 sidecar `.srt`。
合法路径边界与扫盘发现分别由 `src/common/subtitle_paths.py` 的 `ensure_movie_subtitle_path()`
与 `MovieSubtitleService._discover_subtitle_paths()` 收口，二者都覆盖上述三处根。迁移只是把存量整理到新布局
的可选操作，不是硬性前置。

文件命名：统一为 `<番号>-<N>.srt`（N 从当前 `subtitles/` 目录已有序号 max + 1 起），本地导入 / 115 导入 /
存量迁移共用 `src/common/media_paths.py` 的 `allocate_next_movie_subtitle_path()` 分配。同一部影片下多份
字幕（同版本目录里 whisperjav 生成的 chinese/plain 两份、跨版本目录同名 srt、115 云盘多个字幕）都拿到
不同 N，天然不撞车。

导入配对：字幕与视频的配对走**纯番号匹配**——在视频同目录内从字幕文件名解析番号，解析出且与影片番号
一致才配对（不再要求与视频同名），`ABP-123.chs.srt` 等带修饰后缀的字幕也能配上；文件名解析不出番号的
字幕（如 `01.srt`）不纳管。判定收口在 `src/common/movie_numbers.py` 的 `subtitle_matches_movie_number()`，
本地与 115 两条导入路共用。

示例请求：

```http
GET /movies/ABC-001/subtitles
Authorization: Bearer <token>
```

示例响应：

```json
{
  "movie_number": "ABC-001",
  "items": [
    {
      "subtitle_id": 10,
      "file_name": "ABC-001-zh-CN.srt",
      "created_at": "2026-04-07T10:01:00",
      "url": "/files/subtitles/10?expires=1700000900&signature=<signature>"
    }
  ]
}
```

### 手动字幕导入

支持用户把按番号命名的 `.srt` 放进服务器某个目录（浏览白名单 `media_import.browse_roots` 内），
在 GUI 里选择该目录后由后端递归扫描并归档到对应影片的字幕目录。异步执行，进度走
`/system/events/stream`，失败文件支持改名后重导。

**命名规则（v1）**：

- 只接受 `.srt`（后缀大小写不敏感），不支持 `.ass` / `.ssa` / `.vtt`
- 番号必须写在**文件名里**，父目录名不参与识别
- 文件名中必须能解析出一个番号，解析不出则进入失败列表，改名后重导
- 番号以外的内容随意（语种标记、分辨率、字幕组、括号序号、年份等）
- 一个文件只写一个番号：解析器按规则顺序取第一条命中，多个番号结果不可预期

识别口径复用 `src/common/movie_numbers.py` 的 `parse_movie_number_from_text()`，大小写不敏感，
`-` / `_` / 空格分隔都能识别；匹配影片时复用 `find_movie_by_number()`（大小写、分隔符宽松）。
示例：

| 用户命名 | 识别出的番号 | 结果 |
|---|---|---|
| `ABP-123.srt` / `abp-123.srt` | `ABP-123` | 导入 |
| `ABC-001 4K 中文字幕.srt` | `ABC-001` | 导入 |
| `ABC123.srt` / `ABC 123.srt` | `ABC-123` | 导入 |
| `[字幕组] ABP-123.cht.SRT` | `ABP-123` | 导入 |
| `FC2PPV-123456.srt` / `FC2-PPV-123456.srt` | `FC2-123456` | 导入 |
| `01.srt` / `sub.srt` / `中文字幕.srt` | （空） | 失败 |

**导入行为**：

- 递归扫描所选目录（也可直接选单个 `.srt` 文件），只处理 `.srt`，其它文件忽略
- 归档到 `<图片根>/movies/{shard}/{番号}/subtitles/<番号>-<N>.srt` 并登记 `Subtitle`，
  源文件始终保留（硬链接优先、复制兜底，不删源）
- 同一影片已存在相同内容（sha256 相同）的字幕时跳过，不重复导入
- 源文件名里的 `.chs` / `.cht` 等标注**不会**保留到字幕列表，只显示 `<番号>-<N>.srt`
- 解析不出番号 / 库中无对应影片 / 搬运登记异常进入失败列表（`kind=file`，可改名/删除/重导）；
  目录里没有任何 `.srt` 时作业判失败并给出任务级失败原因

接口（均需 Bearer Token）：

- `POST /subtitle-imports`：创建字幕导入作业，body `{"source_path": "<绝对路径>"}`，返回 `202`
- `GET /subtitle-imports`：分页列表
- `GET /subtitle-imports/{subtitle_import_job_id}`：作业详情（含失败文件）
- `POST /subtitle-imports/{subtitle_import_job_id}/retry`：重导失败文件
- `POST /subtitle-imports/{subtitle_import_job_id}/rerun`：整作业重跑
- `DELETE /subtitle-imports/{subtitle_import_job_id}/failed-files`：删除失败源文件
- `POST /subtitle-imports/{subtitle_import_job_id}/failed-files/rename`：重命名失败源文件

### `PUT /movies/{movie_number}/subscription`

- 鉴权：需要 Bearer Token
- Path：
  - `movie_number`：影片番号（字符串）
- 行为：
  - 仅将目标影片的 `is_subscribed` 置为 `true`
  - 若影片原本未订阅或 `subscribed_at` 为空，则写入当前订阅时间
  - 若影片已订阅且已有 `subscribed_at`，则保留原值
- 成功响应：`204 No Content`

错误：

- `404 movie_not_found`：影片不存在

### `DELETE /movies/{movie_number}/subscription`

- 鉴权：需要 Bearer Token
- Path：
  - `movie_number`：影片番号（字符串）
- 行为：
  - 若影片没有任何关联 `media` 记录，则直接取消订阅
  - 若影片存在任意关联 `media` 记录，则拒绝取消订阅
- 成功响应：`204 No Content`

错误：

- `404 movie_not_found`：影片不存在
- `409 movie_subscription_has_media`：影片存在媒体文件，无法取消订阅

### `POST /movies/subscriptions`

- 鉴权：需要 Bearer Token
- 请求体：
  - `movie_numbers`：番号数组（至少 1 项，逐项去空白，禁止空串）
- 行为：
  - 番号按大小写不敏感的精确形态去重匹配（不做 `_`/`-` 互换，宁可 miss 进 `skipped` 也不错标到另一部），逐条判定，采用**部分成功**语义，整体返回 `200`，不因个别条目失败而整批回滚
  - 命中的影片按与单条订阅一致的逻辑置为已订阅：仅在原本未订阅或 `subscribed_at` 为空时写入当前订阅时间，否则保留原值
  - 未在库内命中的番号进入 `skipped`，`reason=movie_not_found`
- 成功响应：`200 OK`

```json
{
  "requested_count": 2,
  "updated_count": 1,
  "skipped_count": 1,
  "skipped": [
    { "movie_number": "NOT-EXIST-999", "reason": "movie_not_found" }
  ]
}
```

字段说明：

- `requested_count`：入参番号总数（去重前）
- `updated_count`：实际置为已订阅的影片条数
- `skipped_count` / `skipped`：本次未处理的条目及原因，`reason` 取值 `movie_not_found`

### `POST /movies/unsubscriptions`

- 鉴权：需要 Bearer Token
- 请求体：
  - `movie_numbers`：番号数组（至少 1 项，逐项去空白，禁止空串）
- 行为：
  - 番号按大小写不敏感的精确形态去重匹配（不做 `_`/`-` 互换，宁可 miss 进 `skipped` 也不错标到另一部），逐条判定，采用**部分成功**语义，整体返回 `200`，不因个别条目失败而整批回滚
  - 命中且没有任何关联 `media` 记录的影片直接取消订阅（`is_subscribed=false`、`subscribed_at=null`）
  - 命中但存在关联 `media` 记录的影片被**跳过**（进入 `skipped`，`reason=has_media`），不报错也不修改，与单条端点"存在媒体文件拒绝取消"语义一致
  - 未在库内命中的番号进入 `skipped`，`reason=movie_not_found`
- 成功响应：`200 OK`

```json
{
  "requested_count": 3,
  "updated_count": 1,
  "skipped_count": 2,
  "skipped": [
    { "movie_number": "ABP-124", "reason": "has_media" },
    { "movie_number": "NOT-EXIST-999", "reason": "movie_not_found" }
  ]
}
```

字段说明：

- `requested_count`：入参番号总数（去重前）
- `updated_count`：实际取消订阅的影片条数
- `skipped_count` / `skipped`：本次未处理的条目及原因，`reason` 取值 `movie_not_found` | `has_media`

### `GET /movies/{movie_number}`

- 鉴权：需要 Bearer Token
- Path：
  - `movie_number`：影片番号（字符串）
- 行为：
  - 返回影片详情、演员列表、标签列表、剧情图列表
  - 演员列表按 `actor.id` 升序
  - 标签列表按 `tag.id` 升序
  - 剧情图按关联表 `movie_plot_image.id` 升序
  - 详情会返回 `maker_name`、`director_name`，但列表接口不会返回这两个字段

示例请求：

```http
GET /movies/ABC-001
```

示例响应：

```json
{
  "id": 1,
  "javdb_id": "MovieA1",
  "movie_number": "ABC-001",
  "title": "Movie 1",
  "series_id": 1,
  "series_name": "Series 1",
  "cover_image": null,
  "thin_cover_image": null,
  "release_date": "2024-01-02",
  "duration_minutes": 120,
  "score": 4.5,
  "watched_count": 12,
  "want_watch_count": 23,
  "comment_count": 34,
  "score_number": 45,
  "heat": 0,
  "is_collection": true,
  "is_subscribed": false,
  "can_play": true,
  "is_4k": false,
  "summary": "summary",
  "maker_name": "S1 NO.1 STYLE",
  "director_name": "嵐山みちる",
  "actors": [
    {
      "id": 1,
      "javdb_id": "ActorA1",
      "name": "三上悠亚",
      "alias_name": "三上悠亚 / 鬼头桃菜",
      "gender": 1,
      "is_subscribed": false,
      "profile_image": null
    }
  ],
  "tags": [
    {
      "tag_id": 1,
      "name": "剧情"
    }
  ],
  "thin_cover_image": null,
  "plot_images": [],
  "media_items": [
    {
      "media_id": 100,
      "library_id": 1,
      "play_url": "/media/100/stream?expires=1700000900&signature=<signature>",
      "storage_mode": "hardlink",
      "resolution": "1920x1080",
      "file_size_bytes": 1073741824,
      "duration_seconds": 7200,
      "special_tags": "普通",
      "valid": true,
      "progress": {
        "last_position_seconds": 600,
        "last_watched_at": "2026-03-08T09:30:00"
      },
      "points": [
        {
          "point_id": 1,
          "thumbnail_id": 5,
          "offset_seconds": 120,
          "image": {
            "id": 88,
            "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
            "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
            "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
            "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>"
          }
        }
      ]
    }
  ]
}
```

## 错误响应格式

统一错误响应：

```json
{
  "error": {
    "code": "movie_not_found",
    "message": "影片不存在",
    "details": {
      "movie_number": "ABC-404"
    }
  }
}
```

常见错误码：

- `movie_not_found`：影片不存在（404）
- `movie_interaction_sync_failed`：影片互动数同步失败（502）
- `movie_javdb_id_missing`：影片缺少 JavDB ID，无法同步互动数（422）
- `movie_heat_recompute_failed`：影片热度重算失败（500）
- `movie_review_fetch_failed`：影片评论拉取失败（502）
- `movie_subscription_has_media`：影片存在媒体文件，无法取消订阅（409）
- `validation_error`：请求参数校验失败（422，例如 `actor_id` 不是整数）

## 兼容性说明

以下旧文档接口在当前 `server/src/api/routers/catalog/movies.py` 中未实现：

- `GET /movies/subscriptions`
- `GET /movies/years`
- `PATCH /movies/{movie_number}`
- `GET /movies/{movie_number}/snapshots`
- `GET /movies/{movie_number}/playlists`
- `GET /movies/{movie_number}/magnets`
- `GET /movies/{movie_number}/points`
