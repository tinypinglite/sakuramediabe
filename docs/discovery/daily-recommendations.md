# 每日推荐

`daily-recommendations` 用于读取最近一次成功生成的每日推荐快照。接口只读快照，不会在请求线程内实时重算推荐。

## 约定

- 数据由定时任务或 CLI `aps generate-daily-recommendations` 生成。
- 系统只保留最近一次成功快照，不保留历史每日推荐。
- 如果今天任务尚未成功执行，接口会继续返回上一批快照，并通过 `is_stale=true` 标记。
- 如果从未生成过快照，接口返回空分页。
- 推荐候选为全库非合集影片，不要求已有可播放媒体；播放能力通过 `can_play` 标记。
- 最近播放影片的相似度信号通过一次 Qdrant 批量查询取得；索引不可用时跳过该信号，热度、榜单、订阅等信号继续参与生成。

## 接口

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/daily-recommendations` | 分页读取最近一次每日推荐快照 |

## GET /daily-recommendations

### Query

| 参数 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `page` | integer | `1` | 页码，从 1 开始 |
| `page_size` | integer | `20` | 每页数量，范围 `1..100` |

### Response

返回 `PageResponse<DailyRecommendationMovieResource>`。

`DailyRecommendationMovieResource` 在影片卡片字段基础上增加：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `snapshot_date` | string | 快照对应的本地日期 |
| `generated_at` | string | 快照生成时间 |
| `rank` | integer | 快照内排序 |
| `recommendation_score` | number | 推荐分 |
| `reason_codes` | string[] | 推荐原因代码 |
| `reason_texts` | string[] | 推荐原因文案 |
| `signal_scores` | object | 各推荐信号分项 |
| `is_stale` | boolean | 快照日期早于今天时为 `true` |

### Example

```http
GET /daily-recommendations?page=1&page_size=20
```
