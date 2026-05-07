# Release: 女优影片年份数量返回

发布日期：2026-05-07

## 变更摘要

`GET /actors/{actor_id}/years` 现在会返回每个发行年份对应的影片数量，方便前端在女优影片列表的年份筛选器中展示数量提示。

## 接口变更

接口：`GET /actors/{actor_id}/years`

响应项新增字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `year` | `int` | 影片发行年份 |
| `movie_count` | `int` | 当前女优在该发行年份下关联的影片数量 |

示例响应：

```json
[
  {
    "year": 2024,
    "movie_count": 18
  },
  {
    "year": 2023,
    "movie_count": 25
  }
]
```

## 统计规则

- 只统计当前女优通过 `movie_actor` 关联到的影片。
- 只统计 `release_date` 非空的影片。
- 数量按发行年份聚合，返回结果按年份倒序排列。

## 兼容性说明

- 这是在现有响应项中新增字段，不新增接口。
- 依赖旧字段 `year` 的客户端可以继续读取年份；需要展示数量的客户端读取 `movie_count`。
- 影片分页列表接口保持不变，选择年份后继续请求 `GET /movies?actor_id={actor_id}&year={year}`。

## 验证

- 已覆盖 service 聚合计数测试：同一年多部影片会正确计数。
- 已覆盖 API 响应测试：年份列表返回 `year` 和 `movie_count`。
- 已确认 `release_date` 为空的影片不会进入年份统计。
