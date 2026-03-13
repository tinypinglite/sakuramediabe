# Status

## 资源说明

状态资源用于返回首页仪表盘需要的全局汇总统计信息，当前仅提供一个只读端点。

## 资源模型

```json
{
  "actors": {
    "female_total": 12,
    "female_subscribed": 8
  },
  "movies": {
    "total": 120,
    "subscribed": 35,
    "playable": 88
  },
  "media_files": {
    "total": 156,
    "total_size_bytes": 9876543210
  },
  "media_libraries": {
    "total": 3
  }
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/status` | 获取系统汇总统计 |

## `GET /status`

需要 Bearer Token。

成功响应：

- `200 OK`: 返回统计资源对象

字段口径：

- `actors.female_total`: `Actor.gender == 1` 的总数
- `actors.female_subscribed`: `Actor.gender == 1` 且 `Actor.is_subscribed == true` 的总数
- `movies.total`: `Movie` 总数
- `movies.subscribed`: `Movie.is_subscribed == true` 的总数
- `movies.playable`: `Media.valid == true` 的媒体所关联的去重影片数量
- `media_files.total`: `Media` 总行数
- `media_files.total_size_bytes`: 所有 `Media.file_size_bytes` 的求和（空库为 `0`）
- `media_libraries.total`: `MediaLibrary` 总数
