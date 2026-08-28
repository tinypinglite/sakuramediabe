# JAV 影片

影片域负责番号、元数据、演员、标签、订阅、黑名单、评论和字幕登记；媒体文件本身属于
playback 域。

## 影片查询

```http
GET  /movies
GET  /movies/latest
POST /movies/by-series
GET  /movies/{movie_number}
GET  /movies/{movie_number}/reviews
GET  /movies/{movie_number}/similar
GET  /movies/{movie_number}/subtitles
```

`GET /movies` 支持演员、标签、年份、收藏类型、番号来源、导演、制作商、热度、
黑名单和分页排序筛选。列表和详情返回影片元数据及媒体摘要；provider 的存储引用不属于
普通 API 响应。

## 元数据与番号

```http
POST /movies/search/parse-number
GET  /movies/search/local?movie_number=...
POST /movies/search/javdb/stream
POST /movies/series/{series_id}/javdb/import/stream
POST /movies/{movie_number}/metadata-refresh
```

JavDB 流式端点以 SSE 返回逐项结果；metadata refresh 只更新目标影片。番号匹配保留库中
原始形态，查询侧才使用大小写和分隔符候选。

## 订阅、黑名单和收藏

```http
POST   /movies/subscriptions
POST   /movies/unsubscriptions
PUT    /movies/{movie_number}/subscription
DELETE /movies/{movie_number}/subscription
PUT    /movies/blacklist
DELETE /movies/blacklist
PATCH  /movies/collection-type
GET    /movies/{movie_number}/collection-status
POST   /movies/{movie_number}/heat-recompute
```

黑名单和订阅操作会在 service 层校验影片状态；热度重算返回异步 TaskRun。

## 字幕

`GET /movies/{movie_number}/subtitles` 返回已登记字幕及签名下载 URL。字幕文件存放在宿主
字幕目录，与具体 Media 记录解耦；文件名按番号和递增序号分配，内容指纹用于去重。
