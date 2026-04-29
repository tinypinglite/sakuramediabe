# Ranking Sources

## 资源说明

排行榜能力使用分层资源建模：

- `ranking sources`：榜单来源站点（如 `javdb`、`missav`）
- `boards`：来源下的榜单（如 `censored`、`uncensored`、`fc2`、`all`）
- `items`：榜单条目（按 `rank` 升序），返回影片列表风格字段并额外包含 `rank`

当前实现特征：

- 所有接口都需要 `Authorization: Bearer <token>`
- API 只读本地已同步数据，不实时请求外部站点
- 榜单数据由定时任务或 CLI `aps sync-rankings` 同步
- `missav` 仅抓取榜单第一页的番号，不翻页
- `missav` 只提供榜单番号，影片详情字段仍统一以 JavDB 数据为准
- 当前开放来源：
  - `javdb`：常规榜 `censored` / `uncensored` / `fc2`
  - `missav`：综合榜 `all`
- 两个来源都支持 `daily` / `weekly` / `monthly`

## 来源与榜单约定

| source_key | 来源名 | board_key | 榜单名 | period |
|---|---|---|---|---|
| `javdb` | JavDB | `censored` | 有码 | `daily` / `weekly` / `monthly` |
| `javdb` | JavDB | `uncensored` | 无码 | `daily` / `weekly` / `monthly` |
| `javdb` | JavDB | `fc2` | FC2 | `daily` / `weekly` / `monthly` |
| `missav` | MissAV | `all` | 综合 | `daily` / `weekly` / `monthly` |

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
  },
  {
    "source_key": "missav",
    "name": "MissAV"
  }
]
```

## GET /ranking-sources/{source_key}/boards

示例：

- `GET /ranking-sources/javdb/boards`
- `GET /ranking-sources/missav/boards`

返回示例：

```json
[
  {
    "source_key": "javdb",
    "board_key": "censored",
    "name": "有码",
    "supported_periods": ["daily", "weekly", "monthly"],
    "default_period": "daily"
  },
  {
    "source_key": "missav",
    "board_key": "all",
    "name": "综合",
    "supported_periods": ["daily", "weekly", "monthly"],
    "default_period": "daily"
  }
]
```

## GET /ranking-sources/{source_key}/boards/{board_key}/items

示例：

- `GET /ranking-sources/javdb/boards/censored/items?period=daily&page=1&page_size=20`
- `GET /ranking-sources/missav/boards/all/items?period=daily&page=1&page_size=20`

### Query 参数

- `period`: 必填（当该榜单支持时间维度时）
- `page`: 可选，默认 `1`
- `page_size`: 可选，默认 `20`

### MissAV 说明

- `missav` 当前只有一个榜单 `all`
- `period=daily` 对应 MissAV 日榜
- `period=weekly` 对应 MissAV 周榜
- `period=monthly` 对应 MissAV 月榜
- 榜单条目的 `movie_number` 来自 MissAV 页面，其他影片详情字段来自本地已同步的 JavDB 入库结果

### 成功响应

返回 `PageResponse[RankedMovieListItemResource]`：

```json
{
  "items": [
    {
      "rank": 1,
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
  "total": 1
}
```

MissAV 返回示例：

```json
{
  "items": [
    {
      "rank": 1,
      "javdb_id": "7ybveb",
      "movie_number": "FNS-196",
      "title": "ヤったら終わりとわかってるのに好きな人の親友が距離感近すぎて巨乳と巨尻の魅力に負けてしまった最低の僕 浜辺やよい 生写真5枚付き",
      "series_name": null,
      "cover_image": null,
      "thin_cover_image": null,
      "release_date": "2026-05-07",
      "duration_minutes": 135,
      "score": 4.2,
      "watched_count": 78,
      "want_watch_count": 393,
      "comment_count": 3,
      "score_number": 471,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 12
}
```

### 错误响应

- `404 ranking_source_not_found`: 来源不存在
- `404 ranking_board_not_found`: 榜单不存在
- `422 invalid_ranking_period`: `period` 缺失或不受支持
