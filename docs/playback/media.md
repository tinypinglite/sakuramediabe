# 媒体

`Media` 是媒体库中一个可播放资产。媒体归属 `Movie`（JAV）或 `VideoItem`（普通视频）
之一；provider 的 `storage_ref` 只在宿主与 provider 之间流转，不出现在普通媒体响应。

## 列表与失效媒体

```http
GET /media
GET /media/invalid
```

`GET /media` 支持 `kind=jav|video|all`、`library_id`、`actor_ids`、
`thumbnail_generation_state`、`sort`、分页参数。列表项包含标题、封面、文件大小、时长、
分辨率、有效性和缩略图状态。

`GET /media/invalid` 返回 provider 对账后标记为不可用的媒体，供宿主处理。

## 播放

```http
GET /media/{media_id}/play/{resource_path:path}?expires=...&signature=...&delivery=auto|proxy|redirect
```

播放 URL 使用统一签名。宿主校验签名并构造 `MediaHandle`，随后把请求交给该媒体所属
provider 的 `handle_playback`；`resource_path` 及响应内容由 provider 解释。provider 不可
用、鉴权失败、来源不存在和上游暂不可用分别返回 `provider_*` 错误。`delivery` 默认 `auto`：
provider 声明支持 `redirect` 时使用 302，否则使用 `proxy`。`auto` 选择 302 后若 provider
返回不支持或可重试错误，宿主只重试一次代理；302 已发出后的客户端播放失败不在后端可见范围内。

## 进度、时刻点和缩略图

```http
PUT    /media/{media_id}/progress
GET    /media/{media_id}/points
POST   /media/{media_id}/points
DELETE /media/{media_id}/points/{point_id}
GET    /media/{media_id}/thumbnails
```

进度只保存秒数和最后观看时间。时刻点必须引用该媒体的缩略图。缩略图状态为
`pending`、`retry_wait`、`terminal` 或 `succeeded`；生成任务通过 provider 的
`generate_thumbnails` 获取产物，宿主只登记结果。

## 删除

```http
DELETE /media/{media_id}
```

删除前由宿主校验关联关系，再调用所属 provider 的 `delete_media`。provider 资源引用不
在 API 响应中暴露。
