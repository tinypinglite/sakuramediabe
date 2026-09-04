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
GET /media/{media_id}/play/{resource_path:path}?expires=...&signature=...&delivery=proxy|redirect
```

播放 URL 使用统一签名。宿主校验签名并构造 `MediaHandle`，随后把请求交给该媒体所属
provider 的 `handle_playback`；`resource_path` 及响应内容由 provider 解释。provider 不可
用、鉴权失败、来源不存在和上游暂不可用分别返回 `provider_*` 错误。

`delivery` 只接受 `proxy` 或 `redirect`；签名播放 URL 使用插件声明的
`playback_deliveries` 首项作为明确的默认值，直接调用网关未传参数时也使用该声明。
宿主不按 provider 名称决定传输方式，不统计 Range 重试，也不在请求失败后改用代理。

115 插件默认 `redirect`：HLS 已就绪时 302 到 115 HLS 播放地址，明确未就绪时
302 到原文件地址。显式 `proxy` 时优先代理 HLS，未就绪时代理原文件并保留 Range。
鉴权、风控和网络错误不等同于转码未就绪。本地插件只支持 `proxy`，读取原文件。

App 合集逐项使用媒体播放地址。JAV 真正合并仍走独立的 merged-play 接口，
固定为代理：115 提供合并 HLS，本地提供虚拟 MP4；115 任一分段无 HLS 时合并不可用。

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
