# Media

## 资源说明

媒体资源代表影片对应的可播放实体。当前播放域已经落地的能力主要包括：

- 视频流访问
- 播放进度上报
- 媒体书签按媒体查询
- 媒体书签添加与删除
- 缩略图列表查询
- 全局媒体书签分页查询
- 媒体删除与有效性管理

当前项目里，媒体详情不会单独通过 `/media/{media_id}` 返回，而是包含在影片详情的 `media_items` 中，详见 [../catalog/movies.md](../catalog/movies.md)。

## 资源模型

播放进度资源：

```json
{
  "media_id": 100,
  "last_position_seconds": 600,
  "last_watched_at": "2026-03-12T10:20:00"
}
```

字段说明补充：

- `special_tags` 中本地媒体的 `4K` 由真实视频流解析得出，不再按文件名、`.iso` 或体积推断
- `valid` 表示媒体记录当前是否对应一个真实存在、可访问的本地文件；巡检会在文件缺失时将其更新为 `false`，文件恢复后再更新回 `true`

媒体书签资源：

```json
{
  "point_id": 10,
  "media_id": 100,
  "thumbnail_id": 5,
  "offset_seconds": 600,
  "image": {
    "id": 88,
    "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>"
  },
  "created_at": "2026-03-12T10:20:00"
}
```

媒体书签列表项：

```json
{
  "point_id": 10,
  "media_id": 100,
  "movie_number": "ABC-001",
  "thumbnail_id": 5,
  "offset_seconds": 600,
  "image": {
    "id": 88,
    "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>"
  },
  "created_at": "2026-03-12T10:20:00"
}
```

媒体缩略图资源：

```json
{
  "thumbnail_id": 5,
  "media_id": 100,
  "offset_seconds": 20,
  "image": {
    "id": 88,
    "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/20.webp?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/20.webp?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/20.webp?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/20.webp?expires=1700000900&signature=<signature>"
  }
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/media-points` | 分页获取全局媒体书签列表 |
| `GET` | `/media/{media_id}/points` | 获取指定媒体的书签列表 |
| `POST` | `/media/{media_id}/points` | 为指定媒体添加书签；重复缩略图幂等返回已有书签 |
| `DELETE` | `/media/{media_id}/points/{point_id}` | 删除指定媒体下的单个书签 |
| `GET` | `/media/{media_id}/stream` | 获取媒体播放流 |
| `PUT` | `/media/{media_id}/progress` | 更新播放进度并维护最近播放 |
| `GET` | `/media/{media_id}/thumbnails` | 获取媒体缩略图列表 |
| `DELETE` | `/media/{media_id}` | 硬删除媒体并清理关联播放数据 |

## 详细接口定义

### Endpoint

`GET /media-points`

### Purpose

分页获取全部 `MediaPoint`。当前接口是全局列表，不按 `media_id` 过滤。

### Auth

需要 Bearer Token。

### Path Params

无。

### Query Params

- `page`: 页码，默认 `1`，必须大于 `0`
- `page_size`: 每页数量，默认 `20`，取值范围 `1-100`
- `sort`: 排序规则，默认 `created_at:desc`

支持的 `sort`：

- `created_at:desc`
- `created_at:asc`

当 `created_at` 相同时，服务端会额外按 `point_id` 同方向排序，保证结果稳定。

### Request Body

无。

### Success Responses

- `200 OK`: 返回分页结果

### Error Responses

- `401 Unauthorized`: 未认证
- `422 Unprocessable Entity`: `page`、`page_size` 或 `sort` 非法

### Example Request

```http
GET /media-points?page=1&page_size=20&sort=created_at:asc
Authorization: Bearer <token>
```

### Example Response

```json
{
  "items": [
    {
      "point_id": 10,
      "media_id": 100,
      "movie_number": "ABC-001",
      "thumbnail_id": 5,
      "offset_seconds": 120,
      "image": {
        "id": 88,
        "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
        "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
        "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
        "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>"
      },
      "created_at": "2026-03-12T10:00:00"
    },
    {
      "point_id": 11,
      "media_id": 101,
      "movie_number": "ABC-002",
      "thumbnail_id": 18,
      "offset_seconds": 360,
      "image": {
        "id": 98,
        "origin": "/files/images/movies/ABC-002/media/fingerprint-2/thumbnails/360.webp?expires=1700000900&signature=<signature>",
        "small": "/files/images/movies/ABC-002/media/fingerprint-2/thumbnails/360.webp?expires=1700000900&signature=<signature>",
        "medium": "/files/images/movies/ABC-002/media/fingerprint-2/thumbnails/360.webp?expires=1700000900&signature=<signature>",
        "large": "/files/images/movies/ABC-002/media/fingerprint-2/thumbnails/360.webp?expires=1700000900&signature=<signature>"
      },
      "created_at": "2026-03-12T11:00:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 2
}
```

### Endpoint

`GET /media/{media_id}/points`

### Purpose

返回指定媒体下的全部 `MediaPoint`。

### Auth

需要 Bearer Token。

### Path Params

- `media_id`: 媒体 ID

### Query Params

无。

### Request Body

无。

### Success Responses

- `200 OK`: 返回书签数组；如果媒体存在但没有书签，则返回空数组

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体不存在

### Behavior

- 返回结果按 `point_id` 升序排列，与影片详情 `media_items[*].points` 的顺序一致
- 仅返回当前 `media_id` 下的书签，不会混入其他媒体的点位

### Example Request

```http
GET /media/100/points
Authorization: Bearer <token>
```

### Example Response

```json
[
  {
    "point_id": 10,
    "media_id": 100,
    "thumbnail_id": 5,
    "offset_seconds": 120,
    "image": {
      "id": 88,
      "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
      "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
      "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>",
      "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/120.webp?expires=1700000900&signature=<signature>"
    },
    "created_at": "2026-03-12T10:00:00"
  },
  {
    "point_id": 12,
    "media_id": 100,
    "thumbnail_id": 8,
    "offset_seconds": 360,
    "image": {
      "id": 90,
      "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/360.webp?expires=1700000900&signature=<signature>",
      "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/360.webp?expires=1700000900&signature=<signature>",
      "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/360.webp?expires=1700000900&signature=<signature>",
      "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/360.webp?expires=1700000900&signature=<signature>"
    },
    "created_at": "2026-03-12T10:30:00"
  }
]
```

### Endpoint

`POST /media/{media_id}/points`

### Purpose

为指定媒体创建书签；若同一媒体下已存在相同 `thumbnail_id`，则按幂等规则返回已有书签。

### Auth

需要 Bearer Token。

### Path Params

- `media_id`: 媒体 ID

### Query Params

无。

### Request Body

```json
{
  "thumbnail_id": 5
}
```

约束：

- `thumbnail_id` 必须大于 `0`

### Success Responses

- `201 Created`: 首次创建成功，返回新建书签资源
- `200 OK`: 该媒体下已存在相同 `thumbnail_id` 的书签，返回已有资源

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体不存在
- `404 Not Found`: `thumbnail_id` 不存在或不属于该媒体（`media_thumbnail_not_found`）
- `422 Unprocessable Entity`: 请求体验证失败

### Behavior

- 幂等维度是 `media_id + thumbnail_id`
- 重复创建不会新增第二条记录
- 当前实现不会自动维护 `MediaProgress`
- 当前实现不会刷新 `recently_played`
- `offset_seconds` 由绑定缩略图的 `offset` 自动确定

### Example Request

```http
POST /media/100/points
Authorization: Bearer <token>
Content-Type: application/json

{
  "thumbnail_id": 5
}
```

### Example Response

```json
{
  "point_id": 20,
  "media_id": 100,
  "thumbnail_id": 5,
  "offset_seconds": 600,
  "image": {
    "id": 88,
    "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/600.webp?expires=1700000900&signature=<signature>"
  },
  "created_at": "2026-03-12T14:00:00"
}
```

### Endpoint

`DELETE /media/{media_id}/points/{point_id}`

### Purpose

删除指定媒体下的单个书签。

### Auth

需要 Bearer Token。

### Path Params

- `media_id`: 媒体 ID
- `point_id`: 书签 ID

### Query Params

无。

### Request Body

无。

### Success Responses

- `204 No Content`: 删除成功

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体不存在，或书签不存在，或书签不属于当前媒体

### Behavior

- 删除只影响单条 `MediaPoint`
- 不会影响 `MediaProgress`
- 不会影响 `recently_played`

### Example Request

```http
DELETE /media/100/points/20
Authorization: Bearer <token>
```

### Endpoint

`GET /media/{media_id}/stream`

### Purpose

按媒体 ID 获取可直接播放的视频字节流。

### Auth

不需要 Bearer Token，但必须提供文件签名。

### Path Params

- `media_id`: 媒体 ID

### Query Params

- `expires`: 签名过期时间戳
- `signature`: 文件签名

### Request Body

无。

### Success Responses

- `200 OK`: 返回完整视频流
- `206 Partial Content`: 返回分段视频流

### Error Responses

- `403 Forbidden`: 缺少签名、签名错误或签名已过期
- `404 Not Found`: 媒体不存在，或媒体记录存在但文件已缺失
- `416 Requested Range Not Satisfiable`: `Range` 请求头非法

### Behavior

- 影片详情中的 `media_items[*].play_url` 就是这个接口返回的签名相对地址
- 前端应使用 `base_url + play_url` 作为播放器地址
- 服务端支持浏览器常见的 `Range` 分段请求
- 成功响应会带上：
  - `Accept-Ranges: bytes`
  - `Content-Length`
  - `Content-Encoding: identity`
  - `Content-Range`（仅 `206` 时返回）

### Example Request

```http
GET /media/100/stream?expires=1700000900&signature=<signature>
Range: bytes=0-1023
```

### Endpoint

`PUT /media/{media_id}/progress`

### Purpose

更新某个媒体的播放进度，并同步维护系统播放列表 `recently_played`。

### Auth

需要 Bearer Token。

### Path Params

- `media_id`: 媒体 ID

### Query Params

无。

### Request Body

```json
{
  "position_seconds": 600
}
```

约束：

- `position_seconds` 必须大于等于 `0`

### Success Responses

- `200 OK`: 返回更新后的播放进度资源

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体不存在
- `422 Unprocessable Entity`: 请求体验证失败

### Behavior

- 若该媒体尚无进度记录，则创建 `MediaProgress`
- 若该媒体已有进度记录，则覆盖 `position_seconds`
- 服务端会将 `last_watched_at` 更新为当前时间
- 服务端会调用播放列表服务刷新该影片在 `recently_played` 中的时间
- 如果影片已经在 `recently_played` 中，不重复插入，只更新时间

### Example Request

```http
PUT /media/100/progress
Authorization: Bearer <token>
Content-Type: application/json

{
  "position_seconds": 600
}
```

### Example Response

```json
{
  "media_id": 100,
  "last_position_seconds": 600,
  "last_watched_at": "2026-03-12T14:00:00"
}
```

### Endpoint

`GET /media/{media_id}/thumbnails`

### Purpose

返回指定媒体的缩略图列表。

### Auth

需要 Bearer Token。

### Path Params

- `media_id`: 媒体 ID

### Query Params

无。

### Request Body

无。

### Success Responses

- `200 OK`: 返回缩略图数组；如果媒体存在但还没有缩略图，则返回空数组

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体不存在

### Behavior

- 返回结果按 `offset_seconds` 升序排列
- `image.origin`、`image.small`、`image.medium`、`image.large` 均为已签名的图片访问相对路径

### Example Request

```http
GET /media/100/thumbnails
Authorization: Bearer <token>
```

### Example Response

```json
[
  {
    "thumbnail_id": 5,
    "media_id": 100,
    "offset_seconds": 10,
    "image": {
      "id": 88,
      "origin": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/10.webp?expires=1700000900&signature=<signature>",
      "small": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/10.webp?expires=1700000900&signature=<signature>",
      "medium": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/10.webp?expires=1700000900&signature=<signature>",
      "large": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/10.webp?expires=1700000900&signature=<signature>"
    }
  }
]
```

### Endpoint

`DELETE /media/{media_id}`

### Purpose

硬删除媒体文件与媒体记录，并清理关联播放数据。

### Auth

需要 Bearer Token。

### Path Params

- `media_id`: 媒体 ID

### Query Params

无。

### Request Body

无。

### Success Responses

- `204 No Content`: 删除成功

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体不存在

### Behavior

- 服务端会尝试删除本地视频文件；如果文件已经不存在，则忽略并继续
- 服务端会删除该媒体记录本身
- 服务端会删除该媒体关联的：
  - `MediaProgress`
  - `MediaPoint`
  - `MediaThumbnail`
  - 无其他引用时，缩略图对应的 `Image`
  - `ResourceTaskState` 中 `resource_type=media` 且 `resource_id` 为当前媒体的任务状态
  - LanceDB 中该媒体关联的缩略图向量
- 不会联动删除 `Movie`
- 不会删除任何 `PlaylistMovie` 关系，包括 `recently_played`

### Example Request

```http
DELETE /media/100
Authorization: Bearer <token>
```

## 当前边界说明

- 当前没有单独的 `GET /media/{media_id}` 接口
- 需要媒体详情、播放地址、进度、书签明细时，应通过影片详情接口读取 `media_items`

## 与“最近播放”列表的联动规则

- “最近播放”是系统播放列表，详见 [../collections/playlists.md](../collections/playlists.md)
- 客户端不需要单独调用播放列表接口去维护“最近播放”
- 只要播放进度更新成功，服务端就会刷新影片在 `recently_played` 中的位置
- 同一部影片只会在 `recently_played` 列表中出现一次
- 同一影片存在多个媒体文件时，任意一个媒体更新进度都会刷新该影片的最近播放时间
- 删除媒体资源不会主动移除影片在 `recently_played` 或其他播放列表中的关系

## 设计备注

- 当前系统是单账号架构，播放进度与书签都不区分多账号
- `GET /media-points` 返回的是全局书签分页视图
- `GET /media/{media_id}/points` 返回的是单媒体书签视图，不支持额外过滤和排序参数
- `POST /media/{media_id}/points` 对相同 `media_id + thumbnail_id` 采用幂等返回已有资源的策略
