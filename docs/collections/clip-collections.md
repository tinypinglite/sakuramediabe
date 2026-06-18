# Clip Collections（片段合集）

## 资源说明

片段合集（`ClipCollection`）是用户自建的、**跨影片的有序片段集合**，用于把多个片段串成一个可连续播放的列表。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

关键设计：

- 合集成员只能是**片段**（`MediaClip`），不放整部影片或时刻。
- 成员有显式 `position` 排序，支持整体重排，前端按顺序连续播放（每个片段是独立文件、各自携带签名 `stream_url`）。
- 同一个片段可同时属于多个合集。
- 删除片段时，会通过外键级联自动把它从所有合集中移除；删除合集只删合集与成员关系，不动片段本体。

与影片播放列表（[playlists.md](./playlists.md)）是两套独立机制：播放列表成员是影片，片段合集成员是片段。

## 资源模型

合集资源（`ClipCollectionResource`）：

```json
{
  "id": 5,
  "name": "连播合集",
  "description": "",
  "clip_count": 3,
  "cover_image": { "id": 1, "origin": "...", "small": "...", "medium": "...", "large": "..." },
  "playlist_url": "/clip-collections/5/playlist.m3u8?expires=...&signature=...",
  "created_at": "2026-06-13T10:00:00",
  "updated_at": "2026-06-13T10:00:00"
}
```

- `cover_image`：取按 `position` 排在最前的片段的封面；空合集为 `null`。
- `playlist_url`：合集 HLS 清单的签名 URL（12 小时有效），供前端 `media_kit` 等播放器作为单一虚拟视频源；空合集为 `null`。

合集成员项（`ClipCollectionClipItemResource`）在片段资源（见 [../playback/media-clips.md](../playback/media-clips.md)）基础上增加 `position`：

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
  "cover_image": { "id": 1, "...": "..." },
  "stream_url": "/media-clips/12/stream?expires=...&signature=...",
  "created_at": "2026-06-13T10:00:00",
  "position": 0
}
```

## 接口

所有接口都需 Bearer Token。

### 合集 CRUD

```
GET    /clip-collections                 列出全部合集（带 clip_count、封面），按更新时间倒序
POST   /clip-collections                 创建合集 { "name": "...", "description": "..." }，201
GET    /clip-collections/{id}            合集详情
PATCH  /clip-collections/{id}            修改 { "name"?, "description"? }
DELETE /clip-collections/{id}            删除合集，204（不删片段本体）
```

- 合集名全局唯一，重名返回 `409 clip_collection_name_conflict`。

### 成员管理

```
GET    /clip-collections/{id}/clips      分页列出成员，按 position 升序，PageResponse
PUT    /clip-collections/{id}/clips/{clip_id}    把片段追加到合集末尾（幂等），204
DELETE /clip-collections/{id}/clips/{clip_id}    从合集移除该片段，204
PUT    /clip-collections/{id}/clips      全量有序设置成员 { "clip_ids": [3, 1, 2] }，204
```

- `PUT /clip-collections/{id}/clips` 用一份有序 `clip_ids` 幂等地设置合集成员与顺序，既覆盖“重排”也覆盖“批量设置成员”。列表内重复的 `clip_id` 按首次出现去重。
- 引用不存在的片段返回 `404 media_clip_not_found`；合集不存在返回 `404 clip_collection_not_found`。

### 连播 HLS 清单（虚拟视频）

```
GET    /clip-collections/{id}/playlist.m3u8?expires=...&signature=...
```

- 走签名 URL，**不要求 Bearer Token**，可由 `media_kit` / `hls.js` / 浏览器原生 `<video>` 直接加载。
- 签名通过 `ClipCollectionResource.playlist_url` 内联下发，12 小时有效。前端无需自行拼装签名。
- 返回 `application/vnd.apple.mpegurl` 文本，VOD 清单（带 `#EXT-X-ENDLIST`）：

  ```
  #EXTM3U
  #EXT-X-VERSION:3
  #EXT-X-PLAYLIST-TYPE:VOD
  #EXT-X-TARGETDURATION:30
  #EXT-X-MEDIA-SEQUENCE:0
  #EXTINF:10.0,
  /media-clips/12/stream?expires=...&signature=...
  #EXT-X-DISCONTINUITY
  #EXTINF:20.0,
  /media-clips/15/stream?expires=...&signature=...
  #EXT-X-ENDLIST
  ```

- 每段分片 URL 复用现有 `/media-clips/{id}/stream` 签名地址，串流由 [`range_streaming`](../playback/media-clips.md) 处理。
- 跨切片恒插入 `#EXT-X-DISCONTINUITY`，确保 decoder 在边界重启、跨段 seek 稳定。
- 空合集或合集内全部切片 `duration_seconds` 为 0 时返回 `404 clip_collection_empty`，前端应在 `playlist_url` 为 `null` 时隐藏播放器入口。
- 签名错误/过期返回 `403 file_signature_invalid` / `file_signature_expired`。

### 合集缩略图轨道

```
GET    /clip-collections/{id}/thumbnails
```

- 把合集时间轴上的所有缩略图按顺序拍平，供前端在进度条悬停时二分定位预览帧。
- 需 Bearer Token。
- 返回 `ClipCollectionThumbnailResource` 数组，按合集 `offset_seconds` 升序：

  ```json
  [
    {
      "collection_id": 5,
      "clip_id": 12,
      "thumbnail_id": 87,
      "offset_seconds": 10,
      "image": { "origin": "...", "small": "...", "medium": "...", "large": "..." },
      "width": 1920,
      "height": 1080
    }
  ]
  ```

- `offset_seconds` 定义为 `sum(前序切片 duration_seconds) + (源缩略图 offset - 切片 start_offset_seconds)`，与 `playlist.m3u8` 的累计时间轴严格一致。
- `width` / `height` 沿用所属源媒体分辨率（同 `/media/{id}/thumbnails`），未探测出分辨率时为 `null`。
- 不分页，与 `/media/{id}/thumbnails` 一致；前端拿到完整数组后做二分查找定位 hover 位置。
