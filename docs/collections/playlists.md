# Playlists

## 资源说明

播放列表资源用于维护影片集合，分为两类：

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

- 系统播放列表：由服务端自动创建和维护，当前内置三个：
  - `recently_played`（显示名“最近播放”）：成员物化存储，客户端播放影片时由服务端自动维护成员和顺序
  - `vr`（显示名“VR”）：虚拟列表，成员不落库，按 `media.special_tags` 含 `VR` 实时派生
  - `4k`（显示名“4K”）：虚拟列表，成员不落库，按 `media.special_tags` 含 `4K` 实时派生
- 自定义播放列表：由用户手动创建、改名、删除，并手动维护影片成员

当前设计中，播放列表成员是影片，不是媒体文件。也就是说同一部影片即使存在多个 `media` 文件，在播放列表里仍只占一条记录。

## 系统默认数据

服务端在执行初始化时，必须幂等地逐个创建以下系统播放列表（老库升级时会自动补建缺失项）：

- `kind=recently_played`，`name=最近播放`
- `kind=vr`，`name=VR`
- `kind=4k`，`name=4K`

这些列表的共同行为约束：

- 不允许客户端创建同名或同 `kind` 的播放列表
- 不允许手动重命名
- 不允许手动删除
- 不允许通过播放列表 API 手动添加 / 移除影片

各自的成员维护方式：

- `recently_played`：成员物化存储于 `playlist_movie`，会在客户端播放影片时由服务端自动维护成员和顺序
- `vr` / `4k`：虚拟列表，成员不落库，每次查询时按 `media.special_tags` 实时派生，因此与影片列表的 `special_tag` 过滤、`is_4k` 标记口径完全一致、永远保持同步
  - 已知限制：VR/4K 成员完全依赖导入时写入的 `media.special_tags`；历史上未回填 `special_tags` 的媒体不会出现在列表中（与现有 `special_tag` 过滤行为一致）

## 资源模型

通用图片结构见 [../catalog/images.md](../catalog/images.md)。

影片摘要（`MovieListItemResource`）见 [../catalog/movies.md](../catalog/movies.md)。

播放列表资源：

```json
{
  "id": 10,
  "name": "最近播放",
  "kind": "recently_played",
  "description": "系统自动维护的最近播放影片列表",
  "is_system": true,
  "is_mutable": false,
  "is_deletable": false,
  "movie_count": 50,
  "created_at": "2026-03-12T10:00:00",
  "updated_at": "2026-03-12T10:00:00"
}
```

字段说明：

- `id`: 播放列表主标识
- `name`: 播放列表显示名称，全局唯一
- `kind`: 播放列表类型，当前支持 `custom`、`recently_played`、`vr`、`4k`
- `description`: 描述信息
- `is_system`: 是否为系统管理列表
- `is_mutable`: 是否允许修改名称和描述
- `is_deletable`: 是否允许删除
- `movie_count`: 列表内影片数量；`vr` / `4k` 虚拟列表为实时统计的 `special_tag` 命中影片数
- `created_at` / `updated_at`: 播放列表资源时间戳

播放列表中的影片项响应在 `MovieListItemResource` 基础上增加 `playlist_item_updated_at`：

```json
{
  "id": 1,
  "javdb_id": "MovieA1",
  "movie_number": "ABC-001",
  "title": "Movie 1",
  "series_id": null,
  "series_name": null,
  "cover_image": null,
  "thin_cover_image": null,
  "release_date": "2024-01-02",
  "duration_minutes": 120,
  "score": 4.5,
  "watched_count": 0,
  "want_watch_count": 0,
  "comment_count": 0,
  "score_number": 0,
  "is_collection": true,
  "is_subscribed": false,
  "can_play": true,
  "playlist_item_updated_at": "2026-03-12T10:20:00"
}
```

字段说明：

- 除 `playlist_item_updated_at` 外，其余字段与 `MovieListItemResource` 保持一致
- `playlist_item_updated_at`: 影片与播放列表关系的最近更新时间
- 对 `recently_played` 列表，该字段表示最近一次播放时间
- 对自定义列表，该字段表示最近一次加入列表的时间
- 对 `vr` / `4k` 虚拟列表，该字段表示该影片最近一次媒体入库时间（即列表排序键）

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/playlists` | 获取播放列表集合 |
| `POST` | `/playlists` | 创建自定义播放列表 |
| `GET` | `/playlists/{playlist_id}` | 获取播放列表详情 |
| `PATCH` | `/playlists/{playlist_id}` | 更新自定义播放列表 |
| `DELETE` | `/playlists/{playlist_id}` | 删除自定义播放列表 |
| `GET` | `/playlists/{playlist_id}/movies` | 分页获取播放列表中的影片（支持排序与分辨率筛选） |
| `GET` | `/playlists/{playlist_id}/resolutions` | 聚合播放列表内影片覆盖的分辨率档位（供筛选下拉渲染） |
| `PUT` | `/playlists/{playlist_id}/movies/{movie_number}` | 将影片加入自定义播放列表 |
| `DELETE` | `/playlists/{playlist_id}/movies/{movie_number}` | 将影片从自定义播放列表移除 |

## 详细接口定义

### `GET /playlists`

- 鉴权：需要 Bearer Token
- Query：
  - `include_system`: 是否包含系统播放列表，默认 `true`
- 行为：
  - 默认返回系统列表和自定义列表
  - 默认排序为：系统列表在前，自定义列表按 `updated_at desc`

示例响应：

```json
[
  {
    "id": 1,
    "name": "最近播放",
    "kind": "recently_played",
    "description": "系统自动维护的最近播放影片列表",
    "is_system": true,
    "is_mutable": false,
    "is_deletable": false,
    "movie_count": 23,
    "created_at": "2026-03-12T10:00:00",
    "updated_at": "2026-03-12T10:00:00"
  },
  {
    "id": 2,
    "name": "我的收藏",
    "kind": "custom",
    "description": "Favorite movies",
    "is_system": false,
    "is_mutable": true,
    "is_deletable": true,
    "movie_count": 8,
    "created_at": "2026-03-12T10:10:00",
    "updated_at": "2026-03-12T11:20:00"
  }
]
```

### `POST /playlists`

- 鉴权：需要 Bearer Token
- 用途：创建自定义播放列表
- 请求体：

```json
{
  "name": "我的收藏",
  "description": "Favorite movies"
}
```

- 规则：
  - 只允许创建 `kind=custom` 的列表
  - `name` 必须全局唯一
  - 系统保留名称与系统保留 `kind` 不允许创建

- 成功响应：
  - `201 Created`

- 错误响应：
  - `401 Unauthorized`
  - `409 Conflict`: `name` 已存在，或使用了系统保留名称
  - `422 Unprocessable Entity`: 字段为空或不合法

### `GET /playlists/{playlist_id}`

- 鉴权：需要 Bearer Token
- Path：
  - `playlist_id`: 播放列表 ID
- 成功响应：
  - `200 OK`
- 错误响应：
  - `401 Unauthorized`
  - `404 Not Found`

### `PATCH /playlists/{playlist_id}`

- 鉴权：需要 Bearer Token
- 用途：更新自定义播放列表名称和描述
- Path：
  - `playlist_id`: 播放列表 ID
- 请求体：至少提供一个字段

```json
{
  "name": "稍后再看",
  "description": "Need watch later"
}
```

- 规则：
  - 系统播放列表不允许修改
  - `name` 仍需保持全局唯一

- 成功响应：
  - `200 OK`

- 错误响应：
  - `401 Unauthorized`
  - `404 Not Found`
  - `409 Conflict`: 名称冲突，或该列表由系统管理不可修改
  - `422 Unprocessable Entity`

### `DELETE /playlists/{playlist_id}`

- 鉴权：需要 Bearer Token
- 用途：删除自定义播放列表
- Path：
  - `playlist_id`: 播放列表 ID

- 成功响应：
  - `204 No Content`

- 错误响应：
  - `401 Unauthorized`
  - `404 Not Found`
  - `409 Conflict`: 该列表由系统管理，不允许删除

### `GET /playlists/{playlist_id}/movies`

- 鉴权：需要 Bearer Token
- Path：
  - `playlist_id`: 播放列表 ID
- Query：
  - `page`: 页码，默认 `1`
  - `page_size`: 每页数量，默认 `20`
  - `sort`: 排序表达式，格式 `field:direction`，`direction` 支持 `asc` / `desc`，可取值：
    - `heat`: 影片热度
    - `bitrate`: 影片全部有效媒体中的最高码率（缺省数据按 0 兜底）
    - `added_at`: 影片最近一次媒体入库时间
    - `release_date`: 影片发布时间（空值统一垫后）
  - `resolution`: 分辨率筛选档位，可取值 `8K` / `4K` / `2K` / `1080P` / `720P` / `480P` / `360P`
- 行为：
  - 返回分页影片摘要列表
  - 每项包含 `playlist_item_updated_at`
  - 不传 `sort` 时的默认排序：
    - `recently_played` / 自定义列表按 `playlist_item_updated_at desc`（最近触达）
    - `vr` / `4k` 虚拟列表按影片最近一次媒体入库时间倒序（`playlist_item_updated_at desc`）
  - `resolution` 筛选为精确档位匹配：按影片最高分辨率媒体归入唯一档位（8K 与 4K 互斥，8K 影片不会命中 `4K`）
  - 非法 `sort` / `resolution` 值返回 `422 invalid_playlist_filter`

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
      "release_date": "2024-01-02",
      "duration_minutes": 120,
      "score": 4.5,
      "watched_count": 0,
      "want_watch_count": 0,
      "comment_count": 0,
      "score_number": 0,
      "is_collection": true,
      "is_subscribed": false,
      "can_play": true,
      "playlist_item_updated_at": "2026-03-12T10:20:00"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### `GET /playlists/{playlist_id}/resolutions`

- 鉴权：需要 Bearer Token
- Path：
  - `playlist_id`: 播放列表 ID
- 行为：
  - 聚合播放列表内影片覆盖的分辨率档位，按档位从高到低返回，每项附命中影片数
  - 每部影片按其最高分辨率媒体归入唯一档位（8K / 4K 互斥）
  - 真实列表按 `PlaylistMovie` 归属统计，`vr` / `4k` 虚拟列表按 `special_tags` 派生，口径与 `movies` 接口的分辨率筛选一致

示例响应：

```json
[
  { "resolution": "8K", "count": 3 },
  { "resolution": "4K", "count": 42 },
  { "resolution": "1080P", "count": 120 }
]
```

### `PUT /playlists/{playlist_id}/movies/{movie_number}`

- 鉴权：需要 Bearer Token
- 用途：将影片加入自定义播放列表
- Path：
  - `playlist_id`: 播放列表 ID
  - `movie_number`: 影片番号
- 行为：
  - 幂等操作
  - 如果影片已在列表中，则不重复创建关系，但会刷新该关系的 `playlist_item_updated_at`
  - 系统播放列表不允许通过该接口写入

- 成功响应：
  - `204 No Content`

- 错误响应：
  - `401 Unauthorized`
  - `404 Not Found`: 播放列表或影片不存在
  - `409 Conflict`: 该列表由系统管理，不允许手动添加影片

### `DELETE /playlists/{playlist_id}/movies/{movie_number}`

- 鉴权：需要 Bearer Token
- 用途：将影片从自定义播放列表移除
- Path：
  - `playlist_id`: 播放列表 ID
  - `movie_number`: 影片番号
- 行为：
  - 幂等操作
  - 即使影片当前不在列表中，也返回成功
  - 系统播放列表不允许通过该接口移除影片

- 成功响应：
  - `204 No Content`

- 错误响应：
  - `401 Unauthorized`
  - `404 Not Found`: 播放列表不存在
  - `409 Conflict`: 该列表由系统管理，不允许手动移除影片

## 与播放行为的联动

- 当客户端对某个媒体执行播放进度上报时，服务端会根据 `media -> movie` 关系，将所属影片自动加入 `recently_played` 播放列表
- 如果影片已存在于 `recently_played` 列表中，不重复插入，只刷新该关系的 `playlist_item_updated_at`
- `recently_played` 列表的排序完全由该时间字段决定，最新播放的影片排在最前面

相关规则详见 [../playback/media.md](../playback/media.md) 中的播放进度接口说明。

## 错误码建议

- `playlist_not_found`: 播放列表不存在
- `playlist_name_conflict`: 播放列表名称冲突
- `playlist_reserved_name`: 使用了系统保留名称
- `playlist_managed_by_system`: 系统播放列表不允许手动修改、删除或维护成员
- `movie_not_found`: 影片不存在
- `invalid_playlist_filter`: 列表影片接口的 `sort` / `resolution` 参数非法

## 设计备注

- 路径中的播放列表主标识统一使用 `playlist_id`
- 对外稳定类型标识使用 `kind`，不要依赖 `name` 判断系统列表
- 当前系统列表有 `recently_played`、`vr`、`4k` 三个；其中 `vr` / `4k` 为按 `special_tags` 实时派生的虚拟列表，成员不落库
- 系统列表的 `kind` 集合由后端 `SYSTEM_PLAYLIST_KINDS` 统一维护；后续如需新增“想看”“继续观看”等系统列表，应复用同一套 `kind + is_system` 设计
