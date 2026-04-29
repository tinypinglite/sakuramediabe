# 演员 API（按当前代码实现）

## 资源说明

当前演员 API 直接面向 `Actor` 实体，不做 `alias_name` 聚合。

- 主标识为 `actor_id`（路径参数，整型）
- `alias_name` 只是演员记录上的普通字段，接口不会按它合并多条演员记录
- 订阅状态 `is_subscribed` 也是单条演员维度，不是分组维度

## 资源模型

通用图片结构见 [images.md](./images.md)。

演员摘要/详情（`ActorResource` / `ActorDetailResource`）结构一致：

```json
{
  "id": 1,
  "javdb_id": "ActorA1",
  "name": "三上悠亚",
  "alias_name": "三上悠亚 / 鬼头桃菜",
  "profile_image": {
    "id": 10,
    "origin": "/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>",
    "small": "/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>",
    "medium": "/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>",
    "large": "/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>"
  },
  "is_subscribed": true
}
```

说明：

- `actor router` 使用字段原名序列化（`response_model_by_alias=False`）
- 演员字段统一为 `snake_case`，如 `javdb_id`、`alias_name`、`is_subscribed`
- `profile_image` 可为 `null`
- `profile_image` 不再返回裸路径，而是带签名的文件访问路径

分页响应模型：

```json
{
  "items": [],
  "page": 1,
  "page_size": 20,
  "total": 0
}
```

说明：

- 请求参数是 `page` / `page_size`
- 响应字段是 `page` / `page_size`

## 接口总览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/actors` | 分页查询演员列表 |
| `GET` | `/actors/search/local` | 本地演员搜索（已废弃，仅保留兼容） |
| `POST` | `/actors/search/javdb/stream` | JavDB 搜索并流式入库（SSE，唯一推荐搜索入口，需登录） |
| `GET` | `/actors/{actor_id}` | 查询演员详情 |
| `PUT` | `/actors/{actor_id}/subscription` | 订阅演员（单条） |
| `DELETE` | `/actors/{actor_id}/subscription` | 取消订阅演员（单条） |
| `GET` | `/actors/{actor_id}/movies` | 查询该演员关联影片（分页） |
| `GET` | `/actors/{actor_id}/movie-ids` | 查询该演员关联影片 ID 列表 |
| `GET` | `/actors/{actor_id}/tags` | 查询该演员关联标签 |
| `GET` | `/actors/{actor_id}/years` | 查询该演员覆盖年份 |

## 详细接口定义

### `GET /actors`

- 鉴权：需要 Bearer Token
- Query：
  - `gender`：性别筛选，默认 `all`
    - `all`：全部演员，包含 `gender = 0` 的未标注数据
    - `female`：仅 `gender = 1`
    - `male`：仅 `gender = 2`
  - `subscription_status`：订阅筛选，默认 `all`
    - `all`：全部演员
    - `subscribed`：仅 `is_subscribed = true`
    - `unsubscribed`：仅 `is_subscribed = false`
  - `sort`：排序表达式（可选，格式 `field:direction`）
    - 支持字段：`subscribed_at`、`name`、`movie_count`
    - 支持方向：`asc`、`desc`
  - `page`：默认 `1`
  - `page_size`：默认 `20`
- 排序：未传 `sort` 时按 `actor.id` 升序；传入 `sort` 时按指定字段和方向排序，并按 `actor.id` 同方向稳定排序；`subscribed_at = null` 固定排在最后

示例请求：

```http
GET /actors?gender=female&subscription_status=subscribed&page=1&page_size=20
```

示例响应：

```json
{
  "items": [
    {
      "id": 1,
      "javdb_id": "ActorA1",
      "name": "三上悠亚",
      "alias_name": "三上悠亚 / 鬼头桃菜",
      "profile_image": null,
      "is_subscribed": true,
      "subscribed_at": "2026-03-08T17:00:00",
      "movie_count": 12
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### `GET /actors/search/local`

已废弃：

- 该接口仅保留兼容，不再作为前端推荐搜索入口
- 当前推荐统一使用 `POST /actors/search/javdb/stream`

- 鉴权：需要 Bearer Token
- Query：
  - `query`：关键词（必填，最小长度 1）
- 匹配规则：
  - 同时检索 `actor.name` 和 `actor.alias_name`
  - 不区分大小写的包含匹配
- 排序：按 `actor.id` 升序
- 成功响应：`200 OK`，响应体为 `ActorResource[]`（非分页）

示例请求：

```http
GET /actors/search/local?query=mikami
Authorization: Bearer <token>
```

示例响应：

```json
[
  {
    "id": 1,
    "javdb_id": "ActorA1",
    "name": "Mikami Yua",
    "alias_name": "三上悠亚 / 鬼头桃菜",
    "profile_image": null,
    "is_subscribed": false
  }
]
```

### `POST /actors/search/javdb/stream`

- 鉴权：需要 Bearer Token
- 推荐用途：当前前端应把它作为唯一女优搜索入口
- 请求体：
  - `actor_name`：女优姓名（必填）
- 响应：
  - `200 OK`
  - `Content-Type: text/event-stream`
  - 业务成功/失败均通过 `completed` 事件判断
- 事件顺序：
  - `search_started`：开始搜索 JavDB
  - `actor_found`：批量返回候选演员
  - `upsert_started`：开始批量入库
  - `image_download_started`：开始下载某个演员头像（逐演员）
  - `image_download_finished`：某个演员头像下载完成（逐演员）
  - `upsert_finished`：批量入库统计
  - `completed`：最终结果（含 `actors: ActorResource[]`）
- 业务规则：
  - JavDB 候选按原始顺序处理，并按 `javdb_id` 去重
  - 候选演员的权威别名来自 JavDB 的 `name`、`name_zht`、`other_name`
  - 入库时会把权威别名自动合并进本地 `alias_name`，不会把用户输入直接写入别名
  - `actor_found[].avatar_url` 优先返回 gfriends CDN 图；gfriends 未命中或异常时回退 JavDB `avatar_url`
  - 按 `CatalogImportService` 既有流程入库演员，并下载头像（若有）
  - 不拉取演员作品
  - 只要有至少一个演员成功入库，`completed.success = true`

示例请求：

```http
POST /actors/search/javdb/stream
Authorization: Bearer <token>
Content-Type: application/json

{
  "actor_name": "三上悠亚"
}
```

成功事件流示例：

```text
event: search_started
data: {"actor_name":"三上悠亚"}

event: actor_found
data: {"actors":[{"javdb_id":"ActorA1","name":"三上悠亚","avatar_url":"https://cdn.jsdelivr.net/gh/xinxin8816/gfriends/%E5%A5%B3%E4%BC%98%E5%A4%B4%E5%83%8F/%E4%B8%89%E4%B8%8A%E6%82%A0%E4%BA%9A.jpg"},{"javdb_id":"ActorA2","name":"鬼头桃菜","avatar_url":null}],"total":2}

event: upsert_started
data: {"total":2}

event: image_download_started
data: {"javdb_id":"ActorA1","index":1,"total":2}

event: image_download_finished
data: {"javdb_id":"ActorA1","index":1,"total":2,"has_avatar":true}

event: image_download_started
data: {"javdb_id":"ActorA2","index":2,"total":2}

event: image_download_finished
data: {"javdb_id":"ActorA2","index":2,"total":2,"has_avatar":false}

event: upsert_finished
data: {"total":2,"created_count":1,"already_exists_count":1,"failed_count":0}

event: completed
data: {"success":true,"actors":[{"id":123,"javdb_id":"ActorA1","name":"三上悠亚","alias_name":"三上悠亚","profile_image":{"id":10,"origin":"/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>","small":"/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>","medium":"/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>","large":"/files/images/actors/ActorA1.jpg?expires=1700000900&signature=<signature>"},"is_subscribed":false},{"id":124,"javdb_id":"ActorA2","name":"鬼头桃菜","alias_name":"鬼头桃菜","profile_image":null,"is_subscribed":false}],"failed_items":[],"stats":{"total":2,"created_count":1,"already_exists_count":1,"failed_count":0}}
```

失败事件流示例（未搜索到）：

```text
event: search_started
data: {"actor_name":"not-exists"}

event: completed
data: {"success":false,"reason":"actor_not_found","actors":[]}
```

### `GET /actors/{actor_id}`

- 鉴权：需要 Bearer Token
- Path：
  - `actor_id`：演员 ID（int）

错误：

- `404 actor_not_found`：演员不存在

### `PUT /actors/{actor_id}/subscription`

- 鉴权：需要 Bearer Token
- 行为：仅将目标 `actor_id` 的 `is_subscribed` 置为 `true`
- 成功响应：`204 No Content`

错误：

- `404 actor_not_found`：演员不存在

### `DELETE /actors/{actor_id}/subscription`

- 鉴权：需要 Bearer Token
- 行为：仅将目标 `actor_id` 的 `is_subscribed` 置为 `false`
- 成功响应：`204 No Content`

错误：

- `404 actor_not_found`：演员不存在

### `GET /actors/{actor_id}/movies`

- 鉴权：需要 Bearer Token
- Query：
  - `special_tag`：按特殊标签过滤（可选，`4k | uncensored | vr`）
  - `page`：默认 `1`
  - `page_size`：默认 `20`
- 行为：
  - 只返回该演员关联的影片
  - `special_tag` 只统计有效本地媒体；例如 `special_tag=4k` 只返回存在有效 `4K` 媒体的影片
  - 按 `movie.movie_number` 升序
  - `total` 为过滤后的影片总数

示例响应：

```json
{
  "items": [
    {
      "javdb_id": "MovieA1",
      "movie_number": "ABC-001",
      "title": "Movie 1",
      "title_zh": "电影 1",
      "cover_image": null,
      "thin_cover_image": null,
      "can_play": true,
      "is_4k": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

错误：

- `404 actor_not_found`：演员不存在

### `GET /actors/{actor_id}/movie-ids`

- 鉴权：需要 Bearer Token
- 返回：该演员关联影片 ID 数组（按 `movie.id` 升序）

示例响应：

```json
[101, 102]
```

错误：

- `404 actor_not_found`：演员不存在

### `GET /actors/{actor_id}/tags`

- 鉴权：需要 Bearer Token
- 返回：标签数组（去重，按 `tag.name` 升序）
- 字段：`tag_id`、`name`

示例响应：

```json
[
  {
    "tag_id": 1,
    "name": "剧情"
  }
]
```

错误：

- `404 actor_not_found`：演员不存在

### `GET /actors/{actor_id}/years`

- 鉴权：需要 Bearer Token
- 返回：年份数组（去重，按年份降序）
- 仅统计 `release_date` 非空影片

示例响应：

```json
[
  {
    "year": 2024
  },
  {
    "year": 2023
  }
]
```

错误：

- `404 actor_not_found`：演员不存在

## 错误响应格式

统一错误响应：

```json
{
  "error": {
    "code": "actor_not_found",
    "message": "演员不存在",
    "details": {
      "actor_id": 123
    }
  }
}
```

常见错误码：

- `unauthorized`：未提供或无效认证（401）
- `actor_not_found`：演员不存在（404）
- `image_download_failed`：演员头像下载失败（仅用于 SSE `completed` 事件）
- `upsert_failed`：JavDB 搜索结果入库失败（仅用于 SSE `completed` 事件）
- `internal_error`：内部异常（仅用于 SSE `completed` 事件）
- `validation_error`：请求参数校验失败（422，例如 `actor_id` 不是整数）

## 兼容性说明

- 旧文档中的 `PATCH /actors/{actor_id}/alias-name` 在当前代码中不存在。
