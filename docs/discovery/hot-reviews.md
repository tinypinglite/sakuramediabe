# Hot Reviews

## 资源说明

`hot-reviews` 用于读取本地已同步的 JavDB 热评快照。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

当前实现特征：

- 所有接口都需要 `Authorization: Bearer <token>`
- API 只读本地已同步数据，不实时请求外部站点
- 热评数据由定时任务或 CLI `aps sync-hot-reviews` 同步
- 同步范围包含 `weekly` / `all` / `quarterly` / `monthly` / `yearly`

## 端点总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/hot-reviews` | 分页读取热评列表 |

## GET /hot-reviews

### Query 参数

- `period`: 可选，默认 `weekly`；支持 `weekly`、`all`、`quarterly`、`monthly`、`yearly`
- `page`: 可选，默认 `1`
- `page_size`: 可选，默认 `20`

### 成功响应

返回 `PageResponse[HotReviewListItemResource]`：

```json
{
  "items": [
    {
      "rank": 1,
      "review_id": 101,
      "score": 5,
      "content": "值得反复看",
      "created_at": "2026-03-21T01:00:00",
      "username": "demo-user",
      "like_count": 11,
      "watch_count": 21,
      "movie": {
        "javdb_id": "javdb-abp001",
        "movie_number": "ABP-001",
        "title": "Movie A",
        "title_zh": "电影 A",
        "series_id": null,
        "series_name": null,
        "cover_image": null,
        "thin_cover_image": null,
        "release_date": null,
        "duration_minutes": 0,
        "score": 0.0,
        "watched_count": 0,
        "want_watch_count": 0,
        "comment_count": 0,
        "score_number": 0,
        "is_collection": false,
        "is_subscribed": false,
        "can_play": false
      }
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

### 错误响应

- `422 invalid_hot_review_period`: `period` 不受支持
