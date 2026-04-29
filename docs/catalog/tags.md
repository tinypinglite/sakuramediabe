# Tags

## 资源说明

标签资源用于标签列表展示，以及按标签查看影片。

## 资源模型

```json
{
  "tag_id": 1,
  "name": "剧情",
  "movie_count": 100
}
```

## 标识符说明

- `tag_id`: 标签主标识

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/tags` | 获取标签列表 |
| `GET` | `/tags/{tag_id}` | 获取标签详情 |
| `GET` | `/tags/{tag_id}/movies` | 获取标签下影片 |

## 详细接口定义

### Endpoint

`GET /tags`

### Purpose

返回标签集合，可用于筛选器初始化。

### Auth

需要 Bearer Token。

### Path Params

无。

### Query Params

- `query`: 可选，按标签名模糊搜索
- `sort`: 排序规则，默认 `movie_count:desc`，支持 `movie_count:asc|desc`、`name:asc|desc`

### Request Body

无。

### Success Responses

- `200 OK`: 返回标签数组

### Error Responses

- `422 Unprocessable Entity`: 查询参数错误

### Example Request

```http
GET /tags?sort=movie_count:desc
```

### Example Response

```json
[
  {
    "tag_id": 1,
    "name": "剧情",
    "movie_count": 100
  }
]
```

### Endpoint

`GET /tags/{tag_id}`

### Purpose

获取单个标签详情。

### Auth

需要 Bearer Token。

### Path Params

- `tag_id`: 标签 ID

### Query Params

无。

### Request Body

无。

### Success Responses

- `200 OK`: 返回标签对象

### Error Responses

- `404 Not Found`: 标签不存在

### Example Request

```http
GET /tags/1
```

### Example Response

```json
{
  "tag_id": 1,
  "name": "剧情",
  "movie_count": 100
}
```

### Endpoint

`GET /tags/{tag_id}/movies`

### Purpose

获取某标签下的影片列表。

### Auth

需要 Bearer Token。

### Path Params

- `tag_id`: 标签 ID

### Query Params

- `year`: 发行年份
- `status`: 按影片状态过滤（可选，`all | subscribed | playable`，默认 `all`）
- `collection_type`: 按合集类型过滤（可选，`all | single`，默认 `all`）
- `special_tag`: 按特殊标签过滤（可选，`4k | uncensored | vr`）
- `director_name`: 按导演名称精确过滤（可选；会先 `strip`）
- `maker_name`: 按厂商名称精确过滤（可选；会先 `strip`）
- `sort`: 排序规则，详见 `GET /movies`
- `page`: 页码
- `page_size`: 每页数量

### Request Body

无。

### Success Responses

- `200 OK`: 返回影片分页列表

### Error Responses

- `422 Unprocessable Entity`: 影片筛选参数错误，例如 `director_name` / `maker_name` 为空白值时返回 `invalid_movie_filter`
- `404 Not Found`: 标签不存在

### Example Request

```http
GET /tags/1/movies?page=1&page_size=20
```

```http
GET /tags/1/movies?director_name=嵐山みちる&maker_name=S1%20NO.1%20STYLE&page=1&page_size=20
```

### Example Response

```json
{
  "items": [
    {
      "javdb_id": "MovieA1",
      "movie_number": "ABC-001",
      "title": "Movie 1",
      "title_zh": "电影 1",
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
      "heat": 0,
      "is_collection": false,
      "is_subscribed": false,
      "can_play": false,
      "is_4k": false
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

## 查询参数说明

- `query`: 标签名称关键词
- `sort`: 排序规则

## 请求/响应示例

```json
{
  "error": {
    "code": "tag_not_found",
    "message": "Tag not found",
    "details": {
      "tag_id": 1
    }
  }
}
```

## 错误语义

- `tag_not_found`: 标签不存在
- `invalid_tag_filter`: 标签过滤参数错误

## 认证要求

本资源默认需要 Bearer Token。

## 设计备注

- 标签是只读目录资源
