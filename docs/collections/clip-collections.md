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
  "created_at": "2026-06-13T10:00:00",
  "updated_at": "2026-06-13T10:00:00"
}
```

- `cover_image`：取按 `position` 排在最前的片段的封面；空合集为 `null`。

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
