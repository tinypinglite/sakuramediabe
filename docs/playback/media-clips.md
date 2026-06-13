# Media Clips（片段）

## 资源说明

片段（`MediaClip`）是用户在某个媒体资源（`media`，单个视频文件）上选两张缩略图圈出一段
`[start_offset, end_offset]` 区间后，由后端用 **ffmpeg 流复制（`-c copy`）同步切出的独立 mp4 文件**。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

关键设计：

- **片段是独立资产，与来源媒体解耦**。删除来源 `media` 不会删除片段：片段的 `media_id` 会被置空（`SET NULL`），但片段记录、产物文件以及 `movie_number` 番号快照都保留，片段仍可播放。
- **区间精度**：用户按缩略图选点，粒度为 10 秒；`-c copy` 切点对齐关键帧，首尾可能偏移几秒。
- **同步生成**：切片在创建请求内完成。为兜住单次耗时、规避接口超时，圈选区间时长不得超过配置 `media.media_clip_max_duration_seconds`（默认 900 秒）。
- **去重幂等**：同一来源媒体的同一 `(media, start, end)` 区间只保留一条；重复创建返回已有片段（HTTP 200）。
- **产物存储**：片段文件存放在独立目录 `media.media_clip_root_path`（默认 `/data/media-clips`），建议作为独立 docker 卷映射到本地持久化，目录需被容器运行用户可写。

## 资源模型

通用图片结构见 [../catalog/images.md](../catalog/images.md)。

片段资源（`MediaClipResource`）：

```json
{
  "clip_id": 12,
  "media_id": 34,
  "movie_number": "ABC-001",
  "start_offset_seconds": 10,
  "end_offset_seconds": 30,
  "title": "精彩片段",
  "duration_seconds": 20,
  "file_size_bytes": 1048576,
  "cover_image": { "id": 1, "origin": "...", "small": "...", "medium": "...", "large": "..." },
  "stream_url": "/media-clips/12/stream?expires=...&signature=...",
  "created_at": "2026-06-13T10:00:00"
}
```

- `media_id`：来源媒体；来源被删除后为 `null`。
- `cover_image`：区间首帧缩略图实时解析；来源媒体/缩略图缺失时为 `null`。
- `stream_url`：内联的签名串流地址，前端直接用于播放（HTTP Range，12 小时有效期）。

片段详情（`MediaClipDetailResource`）在上述字段基础上增加：

```json
{
  "preview_frames": [ { "id": 1, "origin": "...", "...": "..." } ]
}
```

- `preview_frames`：区间内（含首尾）的全部缩略图，按时间升序，供前端循环成动态预览；来源媒体缺失时为空数组。

## 接口

除特别说明外，所有接口都需 Bearer Token。串流接口走签名 URL，不需要 Token。

### 创建片段

```
POST /media/{media_id}/clips
{
  "start_thumbnail_id": 100,
  "end_thumbnail_id": 130,
  "title": "精彩片段"
}
```

- `start_thumbnail_id` / `end_thumbnail_id`：必须是该 `media` 的缩略图；首尾顺序不限，后端取 `min/max` 推出区间。
- 新建返回 `201` + `MediaClipResource`；命中去重返回 `200` + 既有片段。
- 校验失败：两点相同 → `422 media_clip_invalid_range`；区间超长 → `422 media_clip_too_long`；缩略图不属于该媒体 → `404 media_thumbnail_not_found`；切片失败 → `500 media_clip_generation_failed`。

### 列出某媒体的片段

```
GET /media/{media_id}/clips
```

返回 `list[MediaClipResource]`，按创建时间倒序。

### 我的片段（全局分页）

```
GET /media-clips?page=1&page_size=20&sort=created_at:desc
```

返回 `PageResponse[MediaClipResource]`。`sort` 支持 `created_at:desc`（默认）、`created_at:asc`。

### 片段详情

```
GET /media-clips/{clip_id}
```

返回 `MediaClipDetailResource`（含 `preview_frames`）。

### 修改片段标题

```
PATCH /media-clips/{clip_id}
{ "title": "新标题" }
```

返回 `MediaClipResource`。

### 删除片段

```
DELETE /media-clips/{clip_id}
```

成功返回 `204`，同时删除产物文件，并由外键级联把该片段从所有合集中移除。

### 串流播放

```
GET /media-clips/{clip_id}/stream?expires=...&signature=...
```

按 HTTP Range 返回片段文件（`200` 全量或 `206` 部分）。签名无效/过期返回 `403`，文件缺失返回 `404`。
