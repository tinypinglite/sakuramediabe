# Media

## 资源说明

媒体资源代表影片对应的可播放实体。当前播放域已经落地的能力主要包括：

- 视频流访问
- 播放进度上报
- 媒体书签按媒体查询
- 媒体书签添加与删除
- 缩略图列表查询
- 全局媒体书签分页查询
- 全局媒体分页查询（跨 JAV 与 videos 域，支持归属/库/订阅女优筛选与排序）
- 批量把本地媒体秒传到 115，并在成功后删除本地文件
- 媒体删除与有效性管理
- 失效媒体列表查询
- 单个媒体文件有效性复查

当前项目里，媒体详情不会单独通过 `/media/{media_id}` 返回，而是包含在影片详情的 `media_items` 中，详见 [../catalog/movies.md](../catalog/movies.md)。`GET /media` 提供的是跨归属的列表视图，同样不含单条详情接口。

## 资源模型

媒体列表项资源（`GET /media`，跨 JAV 与 videos 域）：

```json
{
  "id": 100,
  "kind": "jav",
  "movie_number": "ABC-001",
  "video_item_id": null,
  "title": "Movie 1",
  "cover_image": {
    "id": 88,
    "origin": "/files/images/movies/ABC-001/cover.webp?expires=1700000900&signature=<signature>",
    "small": "/files/images/movies/ABC-001/cover.webp?expires=1700000900&signature=<signature>",
    "medium": "/files/images/movies/ABC-001/cover.webp?expires=1700000900&signature=<signature>",
    "large": "/files/images/movies/ABC-001/cover.webp?expires=1700000900&signature=<signature>"
  },
  "thin_cover_image": null,
  "library_id": 1,
  "library_name": "Main",
  "path": "/library/main/abc-001.mp4",
  "file_size_bytes": 2147483648,
  "duration_seconds": 5400,
  "resolution": "1920x1080",
  "special_tags": "普通",
  "valid": true,
  "heat": 320,
  "created_at": "2026-03-12T10:20:00",
  "updated_at": "2026-03-12T10:20:00"
}
```

非 JAV（videos 域）媒体：`kind` 为 `video`，`movie_number` 为 `null`，`video_item_id` 非空，`heat`、`thin_cover_image` 恒为 `null`。

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
| `GET` | `/media` | 分页获取全局媒体列表，跨 JAV 与 videos 域，支持归属/库/订阅女优筛选与排序 |
| `POST` | `/media/rapid-uploads` | 异步创建一个批量秒传作业 |
| `GET` | `/media/rapid-uploads` | 分页查询秒传批次 |
| `GET` | `/media/rapid-uploads/{batch_id}` | 查询批次及逐项结果 |
| `POST` | `/media/rapid-uploads/{batch_id}/retry` | 只重试批次中的失败项 |
| `GET` | `/media-points` | 分页获取全局媒体书签列表，按 `kind` 区分 JAV / 非 JAV（默认仅 JAV） |
| `GET` | `/media/invalid` | 分页获取所有失效媒体列表 |
| `POST` | `/media/{media_id}/validity-check` | 单次复查媒体文件是否有效，并同步修正 `valid` |
| `GET` | `/media/{media_id}/points` | 获取指定媒体的书签列表 |
| `POST` | `/media/{media_id}/points` | 为指定媒体添加书签；重复缩略图幂等返回已有书签 |
| `DELETE` | `/media/{media_id}/points/{point_id}` | 删除指定媒体下的单个书签 |
| `GET` | `/media/{media_id}/stream` | 获取媒体播放流 |
| `PUT` | `/media/{media_id}/progress` | 更新播放进度并维护最近播放 |
| `GET` | `/media/{media_id}/thumbnails` | 获取媒体缩略图列表 |
| `DELETE` | `/media/{media_id}` | 硬删除媒体并清理关联播放数据 |

## 详细接口定义

### 批量秒传到 115

`POST /media/rapid-uploads`

请求体：

```json
{
  "media_ids": [101, 102, 103],
  "target_library_id": 8
}
```

- `media_ids` 必须是 1 至 200 个不重复的本地媒体 ID；JAV 和 videos 媒体可以混合提交。
- `target_library_id` 必须指向 `backend=cloud115` 的媒体库。
- 接口只创建后台作业并立即返回 `202 Accepted`：

```json
{
  "rapid_upload_batch_id": 12,
  "task_run_id": 456,
  "status": "accepted"
}
```

批次内部严格按请求顺序逐个秒传。同一目标 115 库会与现有 115 导入共享库级写锁，避免建目录、改名和删除并发。

单项只有在 115 返回秒传成功、并按 `fid/pickcode/SHA1/size` 回查确认后，才会把原 `Media` 原地切换为 cloud115 定位；随后再次核对本地文件的大小、mtime、device 和 inode，确认仍是同一文件后删除。原地切换会保留该媒体已有的播放进度、缩略图、书签、片段与推荐关系。

逐项终态：

- `succeeded`：云端已登记，本地文件已删除。
- `failed`：秒传失败；本地 Media 和文件保持不变。
- `cleanup_failed`：云端已登记，但本地文件未能安全删除；重试时只执行本地清理，不会再次秒传。

前端通过 `GET /system/events/stream` 接收 `task_run_created` / `task_run_updated` 进度事件。批次结束后系统始终创建一条汇总通知：全成功为 `info`，存在任一失败或清理失败为 `warning`。

查询接口：

- `GET /media/rapid-uploads?page=1&page_size=20`
- `GET /media/rapid-uploads/{batch_id}`

重试接口：

`POST /media/rapid-uploads/{batch_id}/retry`

只把原批次的 `failed` / `cleanup_failed` 项放入新批次；没有可重试项时返回 `422 media_rapid_upload_no_retryable_items`。

常见错误：

- `409 media_rapid_upload_conflict`：目标 115 库已有写入任务。
- `409 media_rapid_upload_media_conflict`：所选媒体已在其它秒传批次中。
- `422 media_rapid_upload_source_not_local`：源媒体不是本地媒体。
- `422 media_rapid_upload_target_not_cloud115`：目标不是 115 媒体库。

### Endpoint

`GET /media`

### Purpose

分页获取全局 `Media`，跨 JAV（`movie_number` 非空）与 videos 域（`video_item_id` 非空）统一返回，可按归属、所属媒体库、订阅女优筛选，并支持按文件大小或影片热度排序。

### Auth

需要 Bearer Token。

### Path Params

无。

### Query Params

- `page`: 页码，默认 `1`，必须大于 `0`
- `page_size`: 每页数量，默认 `20`，取值范围 `1-100`
- `kind`: 归属过滤，默认 `all`
- `library_id`: 按所属媒体库过滤，可选
- `actor_ids`: 按订阅女优筛选，逗号分隔的正整数演员 ID 列表，可选；命中任意一位即可（OR 逻辑）。非 JAV 视频没有演员关联，传入该参数后天然被排除，即便 `kind=all`
- `sort`: 排序规则，默认 `created_at:desc`

支持的 `kind`：

- `jav`：仅 JAV 影片媒体
- `video`：仅非 JAV 视频（videos 域）媒体
- `all`（默认）：不限归属，两类混合返回

支持的 `sort`：

- `file_size_bytes:asc` / `file_size_bytes:desc`
- `heat:asc` / `heat:desc`：按所属影片热度排序，仅 JAV 媒体有 `heat`；非 JAV 视频的 `heat` 恒为空，统一排在结果末尾，不受排序方向影响
- 不传时默认按 `created_at:desc`（+ `id` 同方向兜底排序保证稳定）

### Request Body

无。

### Success Responses

- `200 OK`: 返回分页结果，每项为 [`MediaListItemResource`](#资源模型)

### Error Responses

- `401 Unauthorized`: 未认证
- `422 Unprocessable Entity`: `page`、`page_size`、`actor_ids` 或 `sort` 非法（错误码 `invalid_media_filter`）

### Example Request

```http
GET /media?kind=jav&library_id=1&actor_ids=12,34&sort=heat:desc&page=1&page_size=20
Authorization: Bearer <token>
```

### Example Response

```json
{
  "items": [
    {
      "id": 100,
      "kind": "jav",
      "movie_number": "ABC-001",
      "video_item_id": null,
      "title": "Movie 1",
      "cover_image": null,
      "thin_cover_image": null,
      "library_id": 1,
      "library_name": "Main",
      "path": "/library/main/abc-001.mp4",
      "file_size_bytes": 2147483648,
      "duration_seconds": 5400,
      "resolution": "1920x1080",
      "special_tags": "普通",
      "valid": true,
      "heat": 320,
      "created_at": "2026-03-12T10:20:00",
      "updated_at": "2026-03-12T10:20:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### Endpoint

`GET /media-points`

### Purpose

分页获取全局 `MediaPoint`，不按 `media_id` 过滤，但可按归属用 `kind` 筛选。JAV 时刻带 `movie_number`，非 JAV（videos 域）时刻带 `video_item_id`，前端据此区分并跳转对应详情页。

### Auth

需要 Bearer Token。

### Path Params

无。

### Query Params

- `page`: 页码，默认 `1`，必须大于 `0`
- `page_size`: 每页数量，默认 `20`，取值范围 `1-100`
- `sort`: 排序规则，默认 `created_at:desc`
- `kind`: 归属过滤，默认 `jav`

支持的 `sort`：

- `created_at:desc`
- `created_at:asc`

支持的 `kind`：

- `jav`（默认）：仅 JAV 影片媒体的时刻
- `video`：仅非 JAV 视频（videos 域）媒体的时刻
- `all`：不限归属，两类混合返回

`total` 与列表项都会套用同一 `kind` 过滤。当 `created_at` 相同时，服务端会额外按 `point_id` 同方向排序，保证结果稳定。

### Request Body

无。

### Success Responses

- `200 OK`: 返回分页结果

### Error Responses

- `401 Unauthorized`: 未认证
- `422 Unprocessable Entity`: `page`、`page_size`、`sort` 或 `kind` 非法

### Example Request

```http
GET /media-points?page=1&page_size=20&sort=created_at:asc&kind=jav
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
      "video_item_id": null,
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
      "video_item_id": null,
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

`GET /media/invalid`

### Purpose

分页返回当前 `valid=false` 的媒体记录，用于让用户/前端发现并清理失效媒体。

### Auth

需要 Bearer Token。

### Path Params

无。

### Query Params

- `page`: 页码，默认 `1`，必须大于 `0`
- `page_size`: 每页数量，默认 `20`，取值范围 `1-100`
- `search`: 可选，按 `movie_number`、`Movie.title` 或 `Media.path` 模糊匹配

### Request Body

无。

### Success Responses

- `200 OK`: 返回分页结果

### Error Responses

- `401 Unauthorized`: 未认证
- `422 Unprocessable Entity`: `page` 或 `page_size` 非法

### Behavior

- 仅返回 `Media.valid == false` 的记录
- 按 `updated_at` 降序排列（巡检翻 `valid` 时会更新 `updated_at`，因此近似"最近失效优先"）
- `library_id` / `library_name` 在媒体库已被删除（`SET NULL`）时为 `null`
- `movie_title` 在影片未补全标题时可能为 `null`
- `cover_image` / `thin_cover_image` 分别返回影片横版主封面和竖屏封面，影片缺少对应封面时为 `null`
- 失效媒体可通过 `DELETE /media/{media_id}` 清理；删除后该影片若仍订阅且无任何 media 行，下一轮订阅自动下载会重新搜种

### Example Request

```http
GET /media/invalid?page=1&page_size=20&search=ABC
Authorization: Bearer <token>
```

### Example Response

```json
{
  "items": [
    {
      "id": 100,
      "movie_number": "ABC-001",
      "movie_title": "示例标题",
      "cover_image": {
        "id": 10,
        "origin": "/files/images/movies/ABC-001/cover.webp?expires=1760000000&signature=example",
        "small": "/files/images/movies/ABC-001/cover.webp?expires=1760000000&signature=example",
        "medium": "/files/images/movies/ABC-001/cover.webp?expires=1760000000&signature=example",
        "large": "/files/images/movies/ABC-001/cover.webp?expires=1760000000&signature=example"
      },
      "thin_cover_image": {
        "id": 11,
        "origin": "/files/images/movies/ABC-001/thin-cover.webp?expires=1760000000&signature=example",
        "small": "/files/images/movies/ABC-001/thin-cover.webp?expires=1760000000&signature=example",
        "medium": "/files/images/movies/ABC-001/thin-cover.webp?expires=1760000000&signature=example",
        "large": "/files/images/movies/ABC-001/thin-cover.webp?expires=1760000000&signature=example"
      },
      "path": "/media/library/main/ABC-001/v1/ABC-001.mp4",
      "library_id": 1,
      "library_name": "Main Library",
      "file_size_bytes": 2147483648,
      "updated_at": "2026-05-12T03:00:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### Endpoint

`POST /media/{media_id}/validity-check`

### Purpose

同步复查单个媒体文件是否仍然存在，并把数据库中的 `Media.valid` 修正为当前文件状态。适合前端在失效媒体列表中让用户二次确认某条记录。

### Auth

需要 Bearer Token。

### Path Params

- `media_id`: 媒体 ID

### Query Params

无。

### Request Body

无。

### Success Responses

- `200 OK`: 返回本次复查结果

### Error Responses

- `401 Unauthorized`: 未认证
- `404 media_not_found`: 媒体不存在

### Behavior

- 有效性定义与全量 `scan-media-files` 巡检一致：`Media.path` 指向的路径存在且是普通文件
- 该接口不是只读检查；会根据当前文件状态同步修正 `Media.valid`
- 原本 `valid=false` 的媒体如果文件已恢复，会被改回 `valid=true`，随后会从 `GET /media/invalid` 结果中消失
- 巡检只修正 `Media.valid` 并同步影片字幕关系，不探测或补写文件大小、分辨率、时长、`video_info`、特殊标签
- 接口同步执行，不创建后台任务、不写任务中心记录

### Example Request

```http
POST /media/100/validity-check
Authorization: Bearer <token>
```

### Example Response

```json
{
  "id": 100,
  "path": "/media/library/main/ABC-001/v1/ABC-001.mp4",
  "file_exists": true,
  "valid_before": false,
  "valid_after": true,
  "updated": true,
  "invalidated": false,
  "revived": true,
  "checked_at": "2026-05-13T12:00:00"
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

- `200 OK`: 返回完整视频流（本地媒体）
- `206 Partial Content`: 返回分段视频流（本地媒体）
- `302 Found`: 重定向到 115 CDN 直链（cloud115 媒体）

### Error Responses

- `403 Forbidden`: 缺少签名、签名错误或签名已过期
- `404 Not Found`: 媒体不存在，或媒体记录存在但文件已缺失/被 115 封禁
- `416 Requested Range Not Satisfiable`: `Range` 请求头非法
- `422 Unprocessable Entity`: `cloud115_cookies_invalid`（115 cookies 已失效，需重新扫码）
- `429 Too Many Requests`: 115 限流（cloud115 媒体）

### Behavior

- 影片详情中的 `media_items[*].play_url` 就是这个接口返回的签名相对地址
- 前端应使用 `base_url + play_url` 作为播放器地址
- 服务端支持浏览器常见的 `Range` 分段请求（本地媒体）
- 成功响应会带上：
  - `Accept-Ranges: bytes`
  - `Content-Length`
  - `Content-Encoding: identity`
- **cloud115 媒体**：每次请求现拿一条 115 直链后 `302`。直链绑定了**本次请求的 User-Agent**
  （115 CDN 校验后续请求 UA 必须一字不差），播放器跟随 302 时 UA 天然一致；直链带 `t=`
  过期时间（实测十几小时），播放器 seek 触发重新请求 `/stream` 即重新拿链，无需前端特殊处理。
  Flutter media_kit 等自定义 http 客户端需保证跟随重定向时不改写 UA。
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

返回指定媒体的缩略图列表。本接口及 `/media/{media_id}/points`、`/media/{media_id}/progress` 均按 `media_id` 通用，JAV 与非 JAV（videos 域）媒体一致适用；非 JAV 视频前端可从 `GET /videos/{id}` 的 `media_items[].media_id` 取得媒体 ID 后调用。

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
- `width`、`height` 为缩略图像素尺寸，等于所属媒体分辨率（缩略图按视频原始帧无缩放生成，整组一致）；媒体未探测出分辨率时为 `null`

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
    },
    "width": 1280,
    "height": 720
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
  - Qdrant 中该媒体关联的缩略图向量
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
