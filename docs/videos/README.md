# 普通视频

普通视频不使用影片番号、演员或标签语义，`VideoItem` 负责标题和描述，`Media` 负责实际
媒体资产。一个视频条目可以拥有多条媒体和多个合集成员。

## 视频 API

```http
GET    /videos
POST   /videos
GET    /videos/{video_id}
PATCH  /videos/{video_id}
DELETE /videos/{video_id}
```

列表支持 `query`、`sort` 和分页；详情包含合集引用和媒体列表。删除视频条目会按模型外键
规则处理关联媒体。

## 合集

合集 API 位于 `/video-collections`，支持创建、修改、删除、分页列出成员、追加成员和重排
成员。成员关系只引用 `VideoItem`，不会复制媒体 provider 配置。

## Provider 导入

普通视频使用统一 provider 导入接口，不接收本地路径或 provider 专属目录字段：

```http
POST /import-sources/browse
POST /imports
```

请求指定 `media_kind=video`、`library_id`、opaque `source_ref`、可选的
`source_disposition` 和 `collection_id`。storage provider 负责扫描和暂存，宿主事务写入
`VideoItem`/`Media` 后再 finalize；provider 引用不会出现在普通视频响应。
