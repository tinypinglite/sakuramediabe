# 系统状态

所有端点需要 Bearer Token。

## 总览

```http
GET /status
```

返回后端版本、女优统计、影片统计、媒体文件统计、媒体库数量和缩略图统计：

```json
{
  "backend_version": "dev-local",
  "actors": {"female_total": 0, "female_subscribed": 0},
  "movies": {"total": 0, "subscribed": 0, "playable": 0},
  "media_files": {"total": 0, "total_size_bytes": 0},
  "media_libraries": {"total": 0},
  "thumbnails": {
    "pending_media": 0,
    "retry_wait_media": 0,
    "terminal_failed_media": 0,
    "total": 0
  }
}
```

`backend_version` 由 `SAKURAMEDIA_BACKEND_VERSION` 提供，未设置时为 `dev-local`。

## 图像检索状态

```http
GET /status/image-search
```

返回嵌入服务、向量库和缩略图索引的健康状态、检查时间及当前错误。响应不会
包含凭据。

嵌入服务会返回 `space_id`、`dimension` 与支持的模态。更换模型或向量空间后，调用：

```http
POST /image-search/reset
```

该操作会删除缩略图和剧情图的两个向量集合、失效所有搜索会话、把两类图片重新标为待索引，
并分别入队两个索引任务。它是破坏性操作，不能撤销。

## 元数据 provider 测试

```http
GET /status/metadata-providers/javdb/test
```

使用固定番号执行一次真实 JavDB 查询，返回耗时、结果摘要或结构化错误。未知 provider
返回 `422 invalid_metadata_provider`。
