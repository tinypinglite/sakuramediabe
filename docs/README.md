# SakuraMedia API 设计文档

本目录描述 SakuraMedia 服务端的目标 API 设计。

## 全局设计原则

- 通用规范见 [conventions.md](./conventions.md)
- 除登录接口外，所有接口默认要求 `Authorization: Bearer <token>`
- 所有错误响应默认返回统一的 `error` 对象

## 文档导航

- [faq.md](./faq.md): 常见行为说明、自动下载与后台任务说明

### System

- [system/auth.md](./system/auth.md): 登录与访问令牌
- [system/account.md](./system/account.md): 唯一账号资料与密码维护
- [system/config.md](./system/config.md): 统一配置读写接口（全部 toml 配置项）
- [system/indexer-settings.md](./system/indexer-settings.md): 索引器配置管理
- [system/notifications.md](./system/notifications.md): 通知中心接口
- [system/jobs.md](./system/jobs.md): 系统任务元数据与手动触发接口
- [system/task-runs.md](./system/task-runs.md): 任务中心接口
- [system/plugins.md](./system/plugins.md): 插件系统开发指南（目录/zip 插件、契约、任务、扩展点与 CLI/API 管理）

### Catalog

- [catalog/images.md](./catalog/images.md): 通用图片资源与文件访问规则
- [catalog/movies.md](./catalog/movies.md): 影片目录、详情、订阅和关联资源
- [catalog/subscriptions.md](./catalog/subscriptions.md): 影片订阅管理、资源查询状态与重置
- [catalog/actors.md](./catalog/actors.md): 演员目录、订阅和关联资源
- [catalog/tags.md](./catalog/tags.md): 标签目录与标签下影片

### Videos

- [videos/README.md](./videos/README.md): 非 JAV 视频条目、合集与就地导入

### Collections

- [collections/playlists.md](./collections/playlists.md): 播放列表与影片归档
- [collections/clip-collections.md](./collections/clip-collections.md): 跨影片的有序片段合集与连续播放

### Playback

- [playback/media.md](./playback/media.md): 媒体资源、播放流、缩略图、进度和精彩时间点
- [playback/media-clips.md](./playback/media-clips.md): 用户片段（ffmpeg 切片）收藏与串流
- [playback/media-libraries.md](./playback/media-libraries.md): 媒体库配置管理

### Discovery

- [discovery/daily-recommendations.md](./discovery/daily-recommendations.md): 最近一次每日推荐快照分页查询
- [discovery/moment-recommendations.md](./discovery/moment-recommendations.md): 当前推荐时刻池分页查询
- [discovery/image-search.md](./discovery/image-search.md): 以图搜图会话与结果分页
- [discovery/ranking-sources.md](./discovery/ranking-sources.md): 多来源排行榜资源与榜单条目查询

### Transfers

- [transfers/downloads.md](./transfers/downloads.md): 下载器配置与下载任务
- [transfers/media-import.md](./transfers/media-import.md): 统一媒体导入与 TaskRun 语义

### Releases

- [releases/2026-06-13-non-jav-videos-and-clips.md](./releases/2026-06-13-non-jav-videos-and-clips.md): 非 JAV 视频管理与视频片段收藏接口总览
- [releases/2026-05-07-actor-year-movie-count.md](./releases/2026-05-07-actor-year-movie-count.md): 女优影片年份数量返回

### Design proposals

- [provider-plugin-protocol.md](./provider-plugin-protocol.md): 存储、下载与可选缩略图的 Provider 插件协议设计草案
- [provider-file-hash-protocol-draft.md](./provider-file-hash-protocol-draft.md): Provider 媒体文件 Hash 采样约定
- [provider-plugin-progress.md](./provider-plugin-progress.md): Provider 插件化阶段推进记录、变更统计、验证结果与未完成项

### Deployment

- [deployment/docker.md](./deployment/docker.md): Docker 部署教程

## 资源清单

- `auth tokens`
- `account`
- `indexer settings`
- `collection number features`
- `movies`
- `images`
- `actors`
- `tags`
- `playlists`
- `media`
- `media libraries`
- `media points`
- `system jobs`
- `image search sessions`
- `daily recommendations`
- `moment recommendations`
- `ranking sources`
- `download clients`
- `download tasks`
- `video items`
- `video collections`
- `media clips`
- `clip collections`

## 通用认证说明

- 除登录接口和媒体资源(图片、视频、字幕) 外，所有接口都需要 Bearer Token
- 系统只支持一个账号
- 需要登录的业务数据以当前登录会话解释，不再按账号标识分区
