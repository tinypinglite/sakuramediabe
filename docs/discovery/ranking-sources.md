# Ranking Sources

## 资源说明

排行榜能力使用分层资源建模：

- `ranking sources`：榜单来源站点（如 `javdb`）
- `boards`：来源下的榜单（如 `censored`、`uncensored`、`fc2`）
- `items`：榜单条目（按 `rank` 升序），返回影片列表风格字段并额外包含 `rank`

当前实现特征：

- 所有接口都需要 `Authorization: Bearer <token>`
- API 只读本地已同步数据，不实时请求外部站点
- 榜单数据由定时任务或 CLI `aps sync-rankings` 同步
- 当前开放来源：
  - `javdb`：播放榜 `playback_all`（热播）/ `playback_high_score`（高评分），常规榜 `censored` / `uncensored` / `fc2`，TOP250 榜 `top250`
- `daily` / `weekly` / `monthly` 适用于播放榜与常规榜；`top250` 的 period 维度不同（见下）
- `javdb` 的 `top250` 需要登录：依赖 `[metadata]` 的 `javdb_username` / `javdb_password`：
  - 该 board 在 API 列表中**始终可见**，与是否配置账号无关
  - 同步时未配置账号则整 board 跳过抓取（不报错、不发通知、不清空已有数据）
  - 已配置账号但登录失败时跳过 `top250` 并在通知中心发一条 `warning`（每次同步只发一条）

## 来源与榜单约定

| source_key | 来源名 | board_key | 榜单名 | period |
|---|---|---|---|---|
| `javdb` | JavDB | `playback_all` | 热播 | `daily` / `weekly` / `monthly` |
| `javdb` | JavDB | `playback_high_score` | 高评分 | `daily` / `weekly` / `monthly` |
| `javdb` | JavDB | `censored` | 有码 | `daily` / `weekly` / `monthly` |
| `javdb` | JavDB | `uncensored` | 无码 | `daily` / `weekly` / `monthly` |
| `javdb` | JavDB | `fc2` | FC2 | `daily` / `weekly` / `monthly` |
| `javdb` | JavDB | `top250` | TOP250 | `all` / `uncensored` / `censored` / `fc2` / 年份（`2008`~当前年） |

> `playback_all` / `playback_high_score` 为免登录的 JavDB 播放榜，`provider_raw_key` 对应播放榜接口的 `filter_by`（`all` / `high_score`）。
>
> `top250`（需登录）用 `period` 编码不同子榜：`all`（全部）/ `uncensored`（无码）/ `censored`（有码）/ `fc2`，以及 `2008` 到当前年的逐年年度榜（年份维度逐年滚动）。抓取策略：固定子榜与**当前年**每次同步都抓；**历史年份抓一次**，库中已有该年数据则不再每天重抓。

## 端点总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/ranking-sources` | 列出可用榜单来源 |
| `GET` | `/ranking-sources/{source_key}/boards` | 列出来源下可用榜单 |
| `GET` | `/ranking-sources/{source_key}/boards/{board_key}/items` | 分页读取榜单条目 |

## GET /ranking-sources

返回示例：

```json
[
  {
    "source_key": "javdb",
    "name": "JavDB"
  }
]
```

## GET /ranking-sources/{source_key}/boards

示例：

- `GET /ranking-sources/javdb/boards`

返回示例：

```json
[
  {
    "source_key": "javdb",
    "board_key": "censored",
    "name": "有码",
    "supported_periods": ["daily", "weekly", "monthly"],
    "default_period": "daily"
  }
]
```

## GET /ranking-sources/{source_key}/boards/{board_key}/items

示例：

- `GET /ranking-sources/javdb/boards/censored/items?period=daily&page=1&page_size=20`

### Query 参数

- `period`: 必填（当该榜单支持时间维度时）
- `page`: 可选，默认 `1`
- `page_size`: 可选，默认 `20`

### 成功响应

返回 `RankingBoardItemsResource`（在标准分页响应基础上额外带 `synced_at`）：

- `synced_at`: 当前 `source_key + board_key + period` 这批榜单的抓取时间（本地时区字符串）。同步是整榜删旧插新，所以整批共用同一个时间；该榜单+周期暂无数据时为 `null`。

```json
{
  "items": [
    {
      "rank": 1,
      "id": 1,
      "javdb_id": "MovieA1",
      "movie_number": "ABP-001",
      "title": "Movie A",
      "title_zh": "电影 A",
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
  ],
  "page": 1,
  "page_size": 20,
  "total": 1,
  "synced_at": "2026-03-22T06:30:00"
}
```

### 错误响应

- `404 ranking_source_not_found`: 来源不存在
- `404 ranking_board_not_found`: 榜单不存在
- `422 invalid_ranking_period`: `period` 缺失或不受支持
