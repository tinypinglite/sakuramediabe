# Release: 非 JAV 视频管理 + 视频片段收藏

发布日期：2026-06-13

## 变更摘要

本次引入两套相互独立的新能力，共新增 **31 个接口**：

- **非 JAV 视频管理（`videos` 域）**：管理无番号、无外部元数据的视频，按合集组织，并就地导入。`Media` 由「必属 JAV 影片」解耦为「归属 `movie` 或 `video_item` 之一」。
- **视频片段收藏（`MediaClip` + 片段合集）**：在某个媒体上圈选区间，用 ffmpeg 流复制切出独立 mp4 片段；片段可跨影片组成有序合集连续播放。

## 鉴权约定

- 除 `GET /media-clips/{clip_id}/stream`（走签名 URL，参数 `expires` + `signature`，不需 Token）外，**其余全部接口都需要 `Authorization: Bearer <token>`**。
- 分页响应统一为 `PageResponse[T]`：`{ items, page, page_size, total }`。

---

## 一、非 JAV 视频（`videos` 域）

### 1.1 视频条目 `/videos`

| 方法 | 路径 | 作用 | 请求参数 / 体 | 响应 |
| --- | --- | --- | --- | --- |
| GET | `/videos` | 分页列表 | `query`、`sort`、`page`、`page_size` | `PageResponse[VideoItemListItemResource]` |
| POST | `/videos` | 创建条目（201） | `VideoItemCreateRequest` | `VideoItemDetailResource` |
| GET | `/videos/{video_id}` | 详情 | — | `VideoItemDetailResource` |
| PATCH | `/videos/{video_id}` | 局部更新 | `VideoItemUpdateRequest` | `VideoItemDetailResource` |
| DELETE | `/videos/{video_id}` | 删除条目及其媒体（204） | — | — |

- `sort`：`created_at` / `release_date` / `title`，可加 `:asc` / `:desc`。
- PATCH 仅更新 `title` / `summary` / `release_date`，不传则保持原值。

### 1.2 合集 `/video-collections`

| 方法 | 路径 | 作用 | 请求参数 / 体 | 响应 |
| --- | --- | --- | --- | --- |
| GET | `/video-collections` | 全部合集 | — | `List[VideoCollectionResource]` |
| POST | `/video-collections` | 创建（201） | `VideoCollectionCreateRequest` | `VideoCollectionResource` |
| GET | `/video-collections/{collection_id}` | 详情 | — | `VideoCollectionResource` |
| PATCH | `/video-collections/{collection_id}` | 更新 | `VideoCollectionUpdateRequest` | `VideoCollectionResource` |
| DELETE | `/video-collections/{collection_id}` | 删除（204） | — | — |
| GET | `/video-collections/{collection_id}/items` | 成员（按 `position` 升序） | — | `List[VideoCollectionItemResource]` |
| POST | `/video-collections/{collection_id}/items` | 追加成员到末尾（204） | `VideoCollectionItemAddRequest` | — |
| DELETE | `/video-collections/{collection_id}/items/{item_id}` | 移除成员（204） | — | — |
| POST | `/video-collections/{collection_id}/items/reorder` | 按序重写 `position` | `VideoCollectionReorderRequest` | `List[VideoCollectionItemResource]` |

- `reorder` 的 `ordered_item_ids` 须**恰好覆盖全部成员**，否则 `422`。

### 1.3 导入 `/video-imports`

| 方法 | 路径 | 作用 | 请求参数 / 体 | 响应 |
| --- | --- | --- | --- | --- |
| POST | `/video-imports` | 就地索引目录/单文件为 `VideoItem` + `Media`（201） | `VideoImportRequest` | `VideoImportResultResource` |

- **就地索引**：不搬运文件；先按 `Media.path` 跳过已登记，再按**内容指纹**去重（同内容不同路径也跳过）。
- 标题默认取文件名，按需关联 `collection_id`。
- 等价 CLI 见 [../deployment/commands.md](../deployment/commands.md) 的 `import-videos`。

详见 [../videos/README.md](../videos/README.md)。

---

## 二、视频片段（`MediaClip`）

### 2.1 片段 `/media/{media_id}/clips`、`/media-clips`

| 方法 | 路径 | 作用 | 请求参数 / 体 | 响应 |
| --- | --- | --- | --- | --- |
| POST | `/media/{media_id}/clips` | 创建片段（新建 201 / 命中去重 200） | `MediaClipCreateRequest` | `MediaClipResource` |
| GET | `/media/{media_id}/clips` | 列出该媒体的片段（创建时间倒序） | — | `list[MediaClipResource]` |
| GET | `/media-clips` | 我的片段（全局分页） | `page`、`page_size`、`sort` | `PageResponse[MediaClipResource]` |
| GET | `/media-clips/{clip_id}` | 详情（含 `preview_frames`） | — | `MediaClipDetailResource` |
| PATCH | `/media-clips/{clip_id}` | 修改标题 | `MediaClipUpdateRequest` | `MediaClipResource` |
| DELETE | `/media-clips/{clip_id}` | 删除片段及产物文件（204） | — | — |
| GET | `/media-clips/{clip_id}/stream` | 串流播放（**签名 URL，无需 Token**） | `expires`、`signature` | `200` 全量 / `206` 部分 |

- `sort`：`created_at:desc`（默认）/ `created_at:asc`。
- 创建时 `start_thumbnail_id` / `end_thumbnail_id` 须为该 `media` 的缩略图，首尾顺序不限（内部取 `min/max`）。
- `stream_url` 为内联签名地址，默认 12 小时有效，支持 HTTP Range。

### 2.2 片段合集 `/clip-collections`

| 方法 | 路径 | 作用 | 请求参数 / 体 | 响应 |
| --- | --- | --- | --- | --- |
| GET | `/clip-collections` | 全部合集（更新时间倒序） | — | `List[ClipCollectionResource]` |
| POST | `/clip-collections` | 创建（201） | `ClipCollectionCreateRequest` | `ClipCollectionResource` |
| GET | `/clip-collections/{collection_id}` | 详情 | — | `ClipCollectionResource` |
| PATCH | `/clip-collections/{collection_id}` | 更新 | `ClipCollectionUpdateRequest` | `ClipCollectionResource` |
| DELETE | `/clip-collections/{collection_id}` | 删除合集（204，不删片段本体） | — | — |
| GET | `/clip-collections/{collection_id}/clips` | 成员（分页，`position` 升序） | `page`、`page_size` | `PageResponse[ClipCollectionClipItemResource]` |
| PUT | `/clip-collections/{collection_id}/clips/{clip_id}` | 追加片段到末尾（幂等，204） | — | — |
| DELETE | `/clip-collections/{collection_id}/clips/{clip_id}` | 移除片段（204） | — | — |
| PUT | `/clip-collections/{collection_id}/clips` | 全量有序设置成员（重排/批量，204） | `ClipCollectionSetClipsRequest` | — |

详见 [../playback/media-clips.md](../playback/media-clips.md) 与 [../collections/clip-collections.md](../collections/clip-collections.md)。

---

## 三、请求体字段速查

| Schema | 字段 |
| --- | --- |
| `VideoItemCreateRequest` | `title`(必填,自动 strip)、`summary=""`、`release_date?` |
| `VideoItemUpdateRequest` | `title?`、`summary?`、`release_date?` |
| `VideoCollectionCreateRequest` | `name`(必填)、`description=""` |
| `VideoCollectionUpdateRequest` | `name?`、`description?` |
| `VideoCollectionItemAddRequest` | `video_item_id`(>0) |
| `VideoCollectionReorderRequest` | `ordered_item_ids`(成员 `item_id` 列表，非空) |
| `VideoImportRequest` | `source_path`(必填)、`library_id?`、`collection_id?` |
| `MediaClipCreateRequest` | `start_thumbnail_id`(>0)、`end_thumbnail_id`(>0)、`title=""` |
| `MediaClipUpdateRequest` | `title` |
| `ClipCollectionCreateRequest` | `name`(必填)、`description=""` |
| `ClipCollectionUpdateRequest` | `name?`、`description?` |
| `ClipCollectionSetClipsRequest` | `clip_ids`(有序列表，重复按首次出现去重) |

## 四、响应模型字段速查

| Schema | 字段 |
| --- | --- |
| `VideoItemListItemResource` | `id`、`title`、`summary`、`cover_image?`、`release_date?`、`media_count`、`can_play`、`created_at`、`updated_at` |
| `VideoItemDetailResource` | 继承上者 + `media_items[]`（复用影片媒体资源，含进度/时刻/签名播放地址） |
| `VideoCollectionResource` | `id`、`name`、`description`、`item_count`、`created_at`、`updated_at` |
| `VideoCollectionItemResource` | `item_id`、`position`、`video`(VideoItemListItemResource) |
| `VideoImportResultResource` | `created_count`、`skipped_count`、`video_item_ids[]` |
| `MediaClipResource` | `clip_id`、`media_id?`、`movie_number?`、`start_offset_seconds`、`end_offset_seconds`、`title`、`duration_seconds`、`file_size_bytes`、`cover_image?`、`stream_url`、`created_at` |
| `MediaClipDetailResource` | 继承上者 + `preview_frames[]`（区间内所有缩略图） |
| `ClipCollectionResource` | `id`、`name`、`description`、`clip_count`、`cover_image?`、`created_at`、`updated_at` |
| `ClipCollectionClipItemResource` | `MediaClipResource` + `position` |

## 五、主要错误码

| 场景 | 状态码 / code |
| --- | --- |
| 片段两点相同 | `422 media_clip_invalid_range` |
| 片段区间超 `media_clip_max_duration_seconds` | `422 media_clip_too_long` |
| 缩略图不属于该媒体 | `404 media_thumbnail_not_found` |
| 切片失败（含 ffmpeg 超时） | `500 media_clip_generation_failed` |
| 片段不存在 | `404 media_clip_not_found` |
| 片段串流签名无效/过期 | `403 file_signature_invalid` |
| 片段合集重名 | `409 clip_collection_name_conflict` |
| 片段合集不存在 | `404 clip_collection_not_found` |
| 导入源不存在 / 非支持格式 | `404 import_source_not_found` / `422 import_source_unsupported` |
| 关联的库/合集不存在 | `404 media_library_not_found` / `video_collection_not_found` |

## 六、关联文档

- [../videos/README.md](../videos/README.md)：非 JAV 视频域设计与接口详解
- [../playback/media-clips.md](../playback/media-clips.md)：片段资源模型、切片与串流细节
- [../collections/clip-collections.md](../collections/clip-collections.md)：跨影片片段合集与连续播放
- [../deployment/commands.md](../deployment/commands.md)：`import-videos` CLI
