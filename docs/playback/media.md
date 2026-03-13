# Media

## 资源说明

媒体资源代表影片对应的可播放实体。当前播放域已经落地的能力主要包括：

- 视频流访问
- 播放进度上报
- 缩略图列表查询
- 全局媒体书签分页查询
- 媒体软删除

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

媒体书签列表项：

```json
{
  "point_id": 10,
  "media_id": 100,
  "movie_number": "ABC-001",
  "offset_seconds": 600,
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
    "origin": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/00_00_20.webp?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/00_00_20.webp?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/00_00_20.webp?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/media/fingerprint-1/thumbnails/00_00_20.webp?expires=1700000900&signature=<signature>"
  }
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/media-points` | 分页获取全局媒体书签列表 |
| `GET` | `/media/{media_id}/stream` | 获取媒体播放流 |
| `PUT` | `/media/{media_id}/progress` | 更新播放进度并维护最近播放 |
| `GET` | `/media/{media_id}/thumbnails` | 获取媒体缩略图列表 |
| `DELETE` | `/media/{media_id}` | 软删除媒体并清理关联播放数据 |

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
      "offset_seconds": 120,
      "created_at": "2026-03-12T10:00:00"
    },
    {
      "point_id": 11,
      "media_id": 101,
      "movie_number": "ABC-002",
      "offset_seconds": 360,
      "created_at": "2026-03-12T11:00:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 2
}
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
      "origin": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp?expires=1700000900&signature=<signature>",
      "small": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp?expires=1700000900&signature=<signature>",
      "medium": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp?expires=1700000900&signature=<signature>",
      "large": "/files/images/movies/ABC-008/media/fingerprint-1/thumbnails/00_00_10.webp?expires=1700000900&signature=<signature>"
    }
  }
]
```

### Endpoint

`DELETE /media/{media_id}`

### Purpose

删除本地媒体文件，并将媒体记录标记为失效。

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
- 服务端会删除该媒体关联的：
  - `MediaProgress`
  - `MediaPoint`
  - LanceDB 中该媒体关联的缩略图向量
- 服务端会保留该媒体关联的：
  - `Media` 记录本身
  - `MediaThumbnail`
  - 缩略图对应的 `Image`
- 服务端会将 `Media.valid` 更新为 `false`
- 不会联动删除 `Movie`
- 不会删除任何 `PlaylistMovie` 关系，包括 `recently_played`

### Example Request

```http
DELETE /media/100
Authorization: Bearer <token>
```

## 当前边界说明

- 当前没有单独的 `GET /media/{media_id}` 接口
- 当前没有 `GET /media/{media_id}/points`、`POST /media/{media_id}/points`、`DELETE /media/{media_id}/points/{point_id}` 接口
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
- 媒体缩略图删除策略与媒体文件删除策略解耦，删除媒体后仍可保留预览资产
