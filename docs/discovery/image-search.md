# Image Search Sessions

## 资源说明

以图搜图能力采用搜索会话资源建模。客户端上传图片创建会话，再通过结果端点分页读取搜索结果。

当前实现约束：

- 向量模型固定为 JoyTag，不支持切换其他模型
- JoyTag 模型目录通过 `image_search.joytag_model_dir` 配置
- 向量库存储使用本地目录型 LanceDB，由 `lancedb.uri` 与 `lancedb.table_name` 配置
- LanceDB 向量列固定为 `768` 维 `float16`
- 检索距离度量固定为 `cosine`
- LanceDB 默认维护 `movie_id`、`thumbnail_id`、`media_id` 的标量索引
- 向量索引优先使用 `IVF_RQ`，不支持时回退到 `IVF_PQ`
- 运行时优先使用 Intel 核显 (`GPU`)，失败时回退 Intel CPU (`CPU`)
- 当前版本不承诺 AMD CPU 兼容
- 缩略图向量由定时任务 `image_search_index_cron` 增量构建
- 向量索引通过定时任务 `image_search_optimize_cron` 做 `optimize()` 维护，新数据写入后可立即搜索

## 资源模型

```json
{
  "session_id": "d0e808f39d6e460a856d1dbf0f3f6232",
  "status": "ready",
  "expires_at": "2026-03-08T10:10:00Z",
  "page_size": 20,
  "next_cursor": "eyJ2IjoxLCJvZmZzZXQiOjIwfQ",
  "items": [
    {
      "thumbnail_id": 123,
      "media_id": 456,
      "movie_id": 789,
      "movie_number": "ABC-001",
      "offset_seconds": 120,
      "score": 0.91,
      "image": {
        "id": 10,
        "origin": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "small": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "medium": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "large": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=..."
      }
    }
  ]
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/image-search/sessions` | 创建搜索会话并返回第一页结果 |
| `GET` | `/image-search/sessions/{session_id}` | 获取会话元信息 |
| `GET` | `/image-search/sessions/{session_id}/results` | 读取会话结果页 |

## 设计备注

- 搜索会话仍然是临时资源
- 会话不再绑定到某个账号标识
- 访问控制仍通过 Bearer Token 完成
- 结果页查询通过 `cursor` 游标推进
- `score` 为相似度分数，不暴露 LanceDB 原始距离
