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
- `GET /movies/{movie_number}/thumbnails/missav/stream`：按番号流式读取 MissAV seek 缩略图
- `POST /movies/search/javdb/stream`：JavDB 按番号搜索并流式入库
- `POST /movies/series/{series_id}/javdb/import/stream`：按本地系列 ID 抓取 JavDB 系列影片并流式入库
- `POST /movies/{movie_number}/metadata-refresh`：严格刷新本地已有影片的远端元数据
- `POST /movies/{movie_number}/desc-translation`：手动翻译单部影片简介
- `POST /movies/{movie_number}/interaction-sync`：手动同步单部影片互动数
- `POST /movies/{movie_number}/heat-recompute`：手动重算单部影片热度
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
  "javdb_id": "MovieA1",
  "movie_number": "ABC-001",
  "title": "Movie 1",
  "title_zh": "电影 1",
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

影片详情（`MovieDetailResource`）沿用摘要中的 `title_zh`、`cover_image`、`thin_cover_image`，并额外增加：

- `actors`: `MovieActorResource[]`
- `tags`: `TagResource[]`
- `summary`: `string`
- `desc`: `string`（日文原文描述）
- `desc_zh`: `string`（中文翻译描述）
- `maker_name`: `string | null`（厂商名称）
- `director_name`: `string | null`（导演名称）
- `plot_images`: `ImageResource[]`
- `media_items`: `MovieMediaResource[]`
- `cover_image`、`thin_cover_image`、`plot_images` 中的图片字段都返回带签名的文件访问路径

其中：

- `series_name`: 系列名称，可为 `null`
- `series_id`: 系列 ID，可为 `null`；系列名来自独立 `movie_series` 表
- `title`：原始标题
- `title_zh`：中文标题；为空字符串表示尚未翻译
- `thin_cover_image`：优先由封面图裁切生成；若裁切失败，则回退到前两张剧情图中的第一张竖图；若仍未命中则为 `null`
- `heat`: 影片热度值，整数且非空；默认 `0`
- `score`、`score_number`、`watched_count`、`want_watch_count`、`comment_count` 会由定时互动同步任务定期从 JavDB 回刷
- `desc`、`desc_zh`、`maker_name`、`director_name` 仅在详情接口返回，列表接口不返回这些字段

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
  - 由离线任务预计算并写入 `movie_similarity` 表
  - 请求接口只读已落库结果，不在请求线程中临时计算

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
| `GET` | `/movies/{movie_number}/thumbnails/missav/stream` | 流式读取 MissAV seek 缩略图 |
| `POST` | `/movies/search/javdb/stream` | JavDB 按番号搜索并流式入库（SSE） |
| `POST` | `/movies/series/{series_id}/javdb/import/stream` | JavDB 按本地系列 ID 抓取系列影片并流式入库（SSE） |
| `POST` | `/movies/{movie_number}/metadata-refresh` | 严格刷新本地已有影片的远端元数据 |
| `POST` | `/movies/{movie_number}/desc-translation` | 手动翻译单部影片简介 |
| `POST` | `/movies/{movie_number}/interaction-sync` | 手动同步单部影片互动数 |
| `POST` | `/movies/{movie_number}/heat-recompute` | 手动重算单部影片热度 |
| `GET` | `/movies` | 分页查询影片 |
| `GET` | `/movies/latest` | 分页查询最新入库影片 |
| `GET` | `/movies/subscribed-actors/latest` | 分页查询已订阅演员的最新影片 |
| `POST` | `/movies/by-series` | 按本地系列 ID 分页查询影片 |
| `PUT` | `/movies/{movie_number}/subscription` | 订阅影片（单条） |
| `DELETE` | `/movies/{movie_number}/subscription` | 取消订阅影片 |
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
    "title_zh": "电影 1",
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
  - 只读取离线预计算的相似影片结果，按 `rank` 升序返回
  - 响应项复用 `MovieListItemResource`，并附加 `similarity_score`

示例请求：

```http
GET /movies/FC2-123456/similar?limit=5
Authorization: Bearer <token>
```

示例响应：

```json
[
  {
    "javdb_id": "MovieA2",
    "movie_number": "FC2-PPV-654321",
    "title": "Movie 2",
    "title_zh": "电影 2",
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
    - `desc`
    - `desc_zh`
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
  "javdb_id": "MovieA1",
  "movie_number": "ABP-123",
  "title": "Movie 1",
  "title_zh": "电影 1",
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
  "desc": "",
  "desc_zh": "",
  "maker_name": "maker",
  "director_name": "director",
  "thin_cover_image": null,
  "plot_images": [],
  "media_items": [],
  "playlists": []
}
```

### `POST /movies/{movie_number}/desc-translation`

- 鉴权：需要 Bearer Token
- 路径参数：
  - `movie_number`：影片番号
- 行为：
  - 按与 `metadata-refresh` 相同的标准化规则匹配本地影片
  - 只要命中影片，就会重新翻译当前 `desc`，并覆盖已有 `desc_zh`
  - 翻译请求使用共享配置 `movie_info_translation`
  - 成功后返回最新 `MovieDetailResource`
- 错误：
  - `404 movie_not_found`
  - `422 movie_desc_missing`
  - 翻译服务自身返回的错误码会原样透出，例如 `movie_desc_translation_unavailable`

### `POST /movies/{movie_number}/interaction-sync`

- 鉴权：需要 Bearer Token
- 路径参数：
  - `movie_number`：影片番号
- 行为：
  - 按与 `metadata-refresh` 相同的标准化规则匹配本地影片
  - 立即拉取该影片最新 JavDB 互动数，不受批量调度刷新窗口限制
  - 若互动字段发生变化，会同步重算该影片热度
  - 成功后返回最新 `MovieDetailResource`
- 错误：
  - `404 movie_not_found`
  - `422 movie_javdb_id_missing`
  - `502 movie_interaction_sync_failed`

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
data: {"success":true,"movies":[{"javdb_id":"javdb-ABP-123","movie_number":"ABP-123","title":"title-ABP-123","title_zh":"","cover_image":null,"thin_cover_image":null,"release_date":null,"duration_minutes":0,"score":0.0,"watched_count":0,"want_watch_count":0,"comment_count":0,"score_number":0,"is_collection":false,"is_subscribed":false}],"failed_items":[],"stats":{"total":1,"created_count":1,"already_exists_count":0,"failed_count":0}}
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
data: {"success":true,"movies":[{"javdb_id":"javdb-new","movie_number":"ABP-002","title":"New","title_zh":"","cover_image":null,"thin_cover_image":null,"release_date":null,"duration_minutes":0,"score":0.0,"watched_count":0,"want_watch_count":0,"comment_count":0,"score_number":0,"is_collection":false,"is_subscribed":false}],"skipped_items":[{"javdb_id":"javdb-existing","movie_number":"ABP-001","reason":"already_exists"}],"failed_items":[],"stats":{"total":2,"created_count":1,"already_exists_count":1,"failed_count":0}}
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
  - `tag_ids`：按标签 ID 列表过滤（可选，逗号分隔，如 `1,2,3`；命中任意一个标签即可）
  - `director_name`：按导演名称精确过滤（可选；会先 `strip`）
  - `maker_name`：按厂商名称精确过滤（可选；会先 `strip`）
  - `year`：按发行年份过滤（可选，只支持单个年份）
  - `status`：按影片状态过滤（可选，`all | subscribed | playable`，默认 `all`）
  - `collection_type`：按合集类型过滤（可选，`all | single`，默认 `all`；`single` 表示 `is_collection=false`）
  - `special_tag`：按特殊标签过滤（可选，`4k | uncensored | vr`）
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
  - `tag_ids` 只返回至少命中一个指定标签的影片
  - `director_name`、`maker_name` 均为精确匹配，空白值返回 422 `invalid_movie_filter`
  - `year` 只返回 `release_date` 落在该自然年的影片
  - `status=subscribed` 只返回已订阅影片
  - `status=playable` 只返回存在有效媒体的影片
  - `collection_type=single` 只返回 `is_collection=false` 的影片
  - `special_tag=4k` 只返回存在有效 `4K` 媒体的影片；`uncensored`、`vr` 同理

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
GET /movies?tag_ids=1,2&year=2024&page=1&page_size=20
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

### `GET /movies/{movie_number}/thumbnails/missav/stream`

- 鉴权：需要 Bearer Token
- 响应：
  - `200 OK`
  - `Content-Type: text/event-stream`
- Query：
  - `refresh`：是否强制刷新 missav 缓存，默认 `false`
- 行为：
  - 不依赖本地 `Movie` 记录，直接按番号请求 missav 中文页面
  - 只解析播放器 `thumbnail`/`seek` 缩略图配置，不读取剧情图
  - 后端会把 missav 精灵图缓存到本地并切成单张图
  - 中间通过 SSE 推送阶段进度，最终在 `completed` 事件里返回全部签名 URL

示例请求：

```http
GET /movies/SSNI-888/thumbnails/missav/stream?refresh=false
Authorization: Bearer <token>
```

示例事件流：

```text
event: search_started
data: {"movie_number":"SSNI-888","refresh":false}

event: manifest_resolved
data: {"movie_number":"SSNI-888","sprite_total":135,"thumbnail_total":4854}

event: download_started
data: {"total":135}

event: download_progress
data: {"completed":17,"total":135}

event: download_finished
data: {"completed":135,"total":135}

event: slice_started
data: {"total":4854}

event: slice_progress
data: {"completed":325,"total":4854}

event: slice_finished
data: {"completed":4854,"total":4854}

event: completed
data: {"success":true,"result":{"movie_number":"SSNI-888","source":"missav","total":3,"items":[{"index":0,"url":"/files/images/movies/SSNI-888/missav-seek/frames/0.jpg?expires=1700000900&signature=<signature>"}]}}
```

可能的错误码：

- SSE `completed` 事件里的 `reason=missav_thumbnail_not_found`
- SSE `completed` 事件里的 `reason=missav_thumbnail_fetch_failed`

错误：

- `404 movie_not_found`：影片不存在，或 JavDB 评论接口返回 not found
- `502 movie_review_fetch_failed`：JavDB 评论接口请求失败

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
  "javdb_id": "MovieA1",
  "movie_number": "ABC-001",
  "title": "Movie 1",
  "title_zh": "电影 1",
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
- `movie_desc_missing`：影片缺少可翻译的原始简介（422）
- `movie_desc_translation_unavailable`：影片简介翻译服务不可达（503，实际错误码以上游返回为准）
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
