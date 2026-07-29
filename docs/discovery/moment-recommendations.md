# 推荐时刻

推荐时刻用于向客户端展示用户可能喜欢的媒体时间点。服务端只保留最新一次成功生成的推荐池，读取接口不触发生成，保证同一推荐池内分页稳定。

## 端点

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/moment-recommendations` | 分页读取当前推荐时刻池 |

## `GET /moment-recommendations`

- 鉴权：需要 Bearer Token
- Query：
  - `page`：页码，默认 `1`，必须大于 `0`
  - `page_size`：每页数量，默认 `20`，取值范围 `1-100`
- 行为：
  - 只读取当前物化推荐池，不在请求线程中生成或刷新推荐
  - 当前推荐池为空时返回空分页，`generated_at=null`
  - 推荐池由 `generate-moment-recommendations` 任务整体替换；生成失败时保留旧池，成功生成 0 条时清空旧池

响应字段：

- `generated_at`：当前推荐池生成时间；为空表示暂无推荐池
- `items[*].rank`：推荐池内稳定排序
- `items[*].score`：推荐分数
- `items[*].strategy`：推荐来源，当前包括 `visual`、`similar_movie`、`popular`
- `items[*].reason`：展示用推荐理由
- `items[*].media_id` / `thumbnail_id` / `offset_seconds`：播放定位信息
- `items[*].image`：时刻缩略图签名 URL
- `items[*].movie`：影片卡片资源，复用影片列表字段

示例请求：

```http
GET /moment-recommendations?page=1&page_size=20
Authorization: Bearer <token>
```

示例响应：

```json
{
  "items": [
    {
      "recommendation_id": 1,
      "rank": 1,
      "score": 0.88,
      "strategy": "visual",
      "reason": "与你收藏的时刻画面相似",
      "media_id": 100,
      "thumbnail_id": 500,
      "offset_seconds": 360,
      "image": {
        "id": 88,
        "origin": "/files/images/movies/ABC-001/media/thumb.webp?expires=1700000900&signature=<signature>",
        "small": "/files/images/movies/ABC-001/media/thumb.webp?expires=1700000900&signature=<signature>",
        "medium": "/files/images/movies/ABC-001/media/thumb.webp?expires=1700000900&signature=<signature>",
        "large": "/files/images/movies/ABC-001/media/thumb.webp?expires=1700000900&signature=<signature>"
      },
      "movie": {
        "id": 1,
        "javdb_id": "abc-id",
        "movie_number": "ABC-001",
        "title": "Movie title",
        "title_zh": "",
        "series_id": null,
        "series_name": null,
        "cover_image": null,
        "thin_cover_image": null,
        "release_date": null,
        "duration_minutes": 120,
        "score": 0.0,
        "watched_count": 0,
        "want_watch_count": 0,
        "comment_count": 0,
        "score_number": 0,
        "heat": 10,
        "is_collection": false,
        "is_subscribed": false,
        "can_play": true,
        "is_4k": false
      }
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "generated_at": "2026-05-08T04:00:00"
}
```

## 生成任务

单次生成命令：

```bash
uv run python -m src.start.commands aps generate-moment-recommendations
```

生成规则：

- 主召回使用用户收藏的 `MediaPoint` 缩略图做 JoyTag 向量检索
- 视觉召回不足或不可用时，批量查询 Qdrant 影片元数据相似度索引，并按相近播放比例补足
- 影片相似度索引不可用时只跳过该信号，视觉召回与热门候选仍会继续生成
- 没有可用收藏时刻时，从高热度可播放影片中选取精选缩略图补足
- 同一缩略图只保留最高分候选，同一影片最多保留 3 个时刻
