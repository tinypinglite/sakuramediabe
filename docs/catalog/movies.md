# 影片 API（按当前代码实现）

## 资源说明

当前电影路由实现以下能力：

- `POST /movies/search/parse-number`：解析用户输入，提取影片番号
- `GET /movies/search/local`：本地库按番号精确搜索
- `POST /movies/search/javdb/stream`：JavDB 按番号搜索并流式入库
- `GET /movies`：分页查询影片列表
- `GET /movies/latest`：分页查询最新入库影片
- `GET /movies/{movie_number}`：查询影片详情

关键点：

- 影片主标识是 `movie_number`（路径参数）
- 搜索接口只接受 `movie_number` 参数
- 番号搜索使用“标准化后精确匹配”（兼容空白、大小写、`PPV-` 前缀）
- 列表筛选支持 `actor_id` 和 `status`
- 文档字段统一使用 `snake_case`

## 资源模型

通用图片结构见 [images.md](./images.md)。

影片摘要（`MovieListItemResource`）：

```json
{
  "javdb_id": "MovieA1",
  "movie_number": "ABC-001",
  "title": "Movie 1",
  "series_name": "Series 1",
  "cover_image": {
    "id": 10,
    "origin": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/cover.jpg?expires=1700000900&signature=<signature>"
  },
  "release_date": "2024-01-02",
  "duration_minutes": 120,
  "score": 4.5,
  "watched_count": 12,
  "want_watch_count": 23,
  "comment_count": 34,
  "score_number": 45,
  "is_collection": true,
  "is_subscribed": false,
  "can_play": true
}
```

影片详情（`MovieDetailResource`）在摘要基础上增加：

- `actors`: `MovieActorResource[]`
- `tags`: `TagResource[]`
- `summary`: `string`
- `thin_cover_image`: `ImageResource | null`
- `plot_images`: `ImageResource[]`
- `media_items`: `MovieMediaResource[]`
- `cover_image`、`thin_cover_image`、`plot_images` 中的图片字段都返回带签名的文件访问路径

其中：

- `series_name`: 系列名称，可为 `null`

`MovieMediaResource`：

- `media_id`: 媒体 ID
- `library_id`: 媒体库 ID（可空）
- `play_url`: 媒体播放地址；返回带签名的相对 URL，可直接与 `base_url` 拼接访问
- `storage_mode`: 媒体存储模式（可空）
- `resolution`: 分辨率（可空）
- `file_size_bytes`: 文件大小（字节）
- `duration_seconds`: 时长（秒）
- `special_tags`: 特殊标签
- `valid`: 媒体有效性
- `progress`: `MovieMediaProgressResource | null`
- `points`: `MovieMediaPointResource[]`

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
| `POST` | `/movies/search/javdb/stream` | JavDB 按番号搜索并流式入库（SSE） |
| `GET` | `/movies` | 分页查询影片 |
| `GET` | `/movies/latest` | 分页查询最新入库影片 |
| `PUT` | `/movies/{movie_number}/subscription` | 订阅影片（单条） |
| `DELETE` | `/movies/{movie_number}/subscription` | 取消订阅影片（支持强制删除媒体） |
| `GET` | `/movies/{movie_number}` | 查询影片详情 |

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
    "javdb_id": "MovieA1",
    "movie_number": "FC2-PPV-123456",
    "title": "Movie 1",
    "series_name": null,
    "cover_image": null,
    "release_date": null,
    "duration_minutes": 0,
    "score": 0.0,
    "watched_count": 0,
    "want_watch_count": 0,
    "comment_count": 0,
    "score_number": 0,
    "is_collection": false,
    "is_subscribed": false,
    "can_play": false
  }
]
```

### `POST /movies/search/javdb/stream`

- 鉴权：需要 Bearer Token
- 请求体：
  - `movie_number`：影片番号（必填）
- 响应：
  - `200 OK`
  - `Content-Type: text/event-stream`
  - 事件顺序与演员流式接口一致，最终结果看 `completed`
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
data: {"success":true,"movies":[{"javdb_id":"javdb-ABP-123","movie_number":"ABP-123","title":"title-ABP-123","cover_image":null,"release_date":null,"duration_minutes":0,"score":0.0,"watched_count":0,"want_watch_count":0,"comment_count":0,"score_number":0,"is_collection":false,"is_subscribed":false}],"failed_items":[],"stats":{"total":1,"created_count":1,"already_exists_count":0,"failed_count":0}}
```

未找到事件流示例：

```text
event: search_started
data: {"movie_number":"ABP-404"}

event: completed
data: {"success":false,"reason":"movie_not_found","movies":[]}
```

### `GET /movies`

- 鉴权：需要 Bearer Token
- Query：
  - `actor_id`：按演员 ID 过滤（可选）
  - `status`：按影片状态过滤（可选，`all | subscribed | playable`，默认 `all`）
  - `collection_type`：按合集类型过滤（可选，`all | single`，默认 `all`；`single` 表示 `is_collection=false`）
  - `sort`：排序表达式（可选，格式 `field:direction`）
    - `field` 支持：`release_date`、`added_at`、`subscribed_at`、`comment_count`、`score_number`、`want_watch_count`、`heat`
    - `direction` 支持：`asc | desc`
  - `page`：默认 `1`
  - `page_size`：默认 `20`
- 行为：
  - 未传 `sort` 时，按 `movie.movie_number` 升序
  - 传入 `sort` 时，按指定字段和方向排序；若主排序值相同，则按 `movie.id` 同方向稳定排序
  - `release_date`、`subscribed_at` 为空的影片始终排在最后
  - `total` 为过滤后的影片总数
  - `status=subscribed` 只返回已订阅影片
  - `status=playable` 只返回存在有效媒体的影片
  - `collection_type=single` 只返回 `is_collection=false` 的影片

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
GET /movies?collection_type=single&sort=release_date:desc&page=1&page_size=20
```

示例响应：

```json
{
  "items": [
    {
      "javdb_id": "MovieA1",
      "movie_number": "ABC-001",
      "title": "Movie 1",
      "series_name": null,
      "cover_image": null,
      "release_date": null,
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false
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
      "javdb_id": "MovieA2",
      "movie_number": "ABC-002",
      "title": "Movie 2",
      "series_name": null,
      "cover_image": null,
      "release_date": null,
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": true
    },
    {
      "javdb_id": "MovieA1",
      "movie_number": "ABC-001",
      "title": "Movie 1",
      "series_name": null,
      "cover_image": null,
      "release_date": null,
      "duration_minutes": 0,
      "score": 0.0,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 2
}
```

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
- Query：
  - `delete_media`：是否强制删除媒体，默认 `false`
- 行为：
  - 若影片没有任何关联 `media` 记录，则直接取消订阅
  - 若影片存在任意关联 `media` 记录，且 `delete_media=false`，则拒绝取消订阅
  - 仅当 `delete_media=true` 时，才允许继续取消订阅，并执行：
    - 删除该影片关联的全部本地媒体文件
    - 文件不存在时忽略
    - 将该影片关联的全部 `media.valid` 置为 `false`
    - 将影片 `is_subscribed` 置为 `false`
    - 将 `subscribed_at` 置为 `null`
  - 强制取消订阅不会删除 `media` 记录本身
- 成功响应：`204 No Content`

错误：

- `404 movie_not_found`：影片不存在
- `409 movie_subscription_has_media`：影片存在媒体文件，若需取消订阅请传 `delete_media=true`

### `GET /movies/{movie_number}`

- 鉴权：需要 Bearer Token
- Path：
  - `movie_number`：影片番号（字符串）
- 行为：
  - 返回影片详情、演员列表、标签列表、剧情图列表
  - 演员列表按 `actor.id` 升序
  - 标签列表按 `tag.id` 升序
  - 剧情图按关联表 `movie_plot_image.id` 升序

示例请求：

```http
GET /movies/ABC-001
```

示例响应：

```json
{
  "javdb_id": "MovieA1",
  "movie_number": "ABC-001",
  "title": "Movie 1",
  "series_name": "Series 1",
  "cover_image": null,
  "release_date": "2024-01-02",
  "duration_minutes": 120,
  "score": 4.5,
  "watched_count": 12,
  "want_watch_count": 23,
  "comment_count": 34,
  "score_number": 45,
  "is_collection": true,
  "is_subscribed": false,
  "can_play": true,
  "summary": "summary",
  "actors": [
    {
      "id": 1,
      "javdb_id": "ActorA1",
      "name": "三上悠亚",
      "alias_name": "三上悠亚 / 鬼头桃菜",
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
          "offset_seconds": 120
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
- `movie_subscription_has_media`：影片存在媒体文件，若需取消订阅请传 `delete_media=true`（409）
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
