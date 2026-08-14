# Ranking Sources

## 资源说明

排行榜能力使用分层资源建模：

- `ranking sources`：榜单来源站点（如 `javdb`）
- `boards`：来源下的榜单（如 `censored`、`uncensored`、`fc2`）
- `items`：榜单条目（按 `rank` 升序），返回影片列表风格字段并额外包含 `rank`

当前实现特征：

- 所有接口都需要 `Authorization: Bearer <token>`
- API 只读本地已同步数据，不实时请求外部站点
- **排行榜不是默认功能**：宿主不内置任何榜单来源。未安装排行榜插件时，
  `GET /ranking-sources` 返回空列表
- 榜单来源由**排行榜插件**提供：插件通过 `discovery.ranking_source` 扩展点
  声明 `source_key` / boards / 抓取回调，宿主注册后由插件的同步任务驱动，
  写入 `RankingItem`，API 与前端查询协议不变
- 同步节奏由排行榜插件自己注册的定时/手动任务驱动，宿主不再提供
  `aps sync-rankings` 命令
- 需登录榜单（如 JavDB TOP250）的账号由插件在自己的
  `plugins.settings.<plugin_id>` 中管理，宿主不感知账号配置
- `source_key` 是稳定领域标识（如官方 JavDB 插件使用 `javdb`），由插件声明、
  全局唯一；两个插件声明相同 `source_key` 时，后加载的插件整插件隔离
- `GET /ranking-sources` 返回的来源条目带 `plugin_id` 字段，标识来源由哪个插件提供

插件排行榜来源的完整契约见 [插件系统开发指南](../system/plugins.md)。

## 端点总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/ranking-sources` | 列出可用榜单来源 |
| `GET` | `/ranking-sources/{source_key}/boards` | 列出来源下可用榜单 |
| `GET` | `/ranking-sources/{source_key}/boards/{board_key}/items` | 分页读取榜单条目 |

## GET /ranking-sources

返回示例（安装了官方 `javdb_ranking` 插件时）：

```json
[
  {
    "source_key": "javdb",
    "name": "JavDB",
    "plugin_id": "javdb_ranking"
  }
]
```

未安装任何排行榜插件时返回 `[]`。

## GET /ranking-sources/{source_key}/boards

返回该来源下插件声明的榜单：

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
