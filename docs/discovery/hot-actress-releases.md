# 热门女优新片

## 资源说明

`hot-actress-releases` 用于发现刚发布、尚未积累自身热度，但出演女优已有稳定热门作品表现的影片。

- 所有接口都需要 `Authorization: Bearer <token>`
- 接口实时读取本地影片、演员和互动热度数据，不请求外部站点
- 结果按推荐分数分页；同一女优的多部候选新片会同时保留

## 端点总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/hot-actress-releases` | 分页读取热门女优的新片 |

## 推荐规则

- 候选片：非合集、至少有一位女性演员，发布时间在当前日期前 90 天至后 90 天内
- 女优历史证据：非合集、发布时间在当前日期前 180 天至前 60 天内、且影片恰好只有一位女性演员
- 每位女优至少需要 3 部有效历史片；候选片若本身落在历史窗口，会从自己的历史证据中排除
- 单部历史片证据为 `log(1 + heat / max(发布时间至今的天数, 60))`
- 女优分数为全部有效历史片证据的平均值；候选片取出演女性演员中的最高分
- 排序为 `recommendation_score DESC`、`release_date DESC`、`movie.id DESC`

这里的 `recommendation_score` 是发现新片的推荐信号，不会写回或改变影片自身的 `heat`。

## GET /hot-actress-releases

### Query 参数

- `page`: 可选，默认 `1`，最小为 `1`
- `page_size`: 可选，默认 `20`，范围为 `1` 至 `100`

### 成功响应

```json
{
  "items": [
    {
      "id": 1,
      "movie_number": "ABC-001",
      "title": "Movie A",
      "release_date": "2026-08-20",
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false,
      "is_4k": false,
      "recommendation_score": 3.4128,
      "hot_actress": {
        "id": 12,
        "name": "Actress A",
        "profile_image": null,
        "historical_movie_count": 4,
        "hotness_score": 3.4128
      }
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```
