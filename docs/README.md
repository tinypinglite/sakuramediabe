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
- [system/indexer-settings.md](./system/indexer-settings.md): 索引器配置管理
- [system/metadata-provider-license.md](./system/metadata-provider-license.md): 闭源元数据 Provider 授权状态与激活
- [system/collection-number-features.md](./system/collection-number-features.md): 合集影片番号特征管理
- [system/notifications.md](./system/notifications.md): 通知中心接口
- [system/jobs.md](./system/jobs.md): 系统任务元数据与手动触发接口
- [system/task-runs.md](./system/task-runs.md): 任务中心与事件流接口
- [system/flutter-activity-integration.md](./system/flutter-activity-integration.md): Flutter 活动中心对接说明

### Catalog

- [catalog/images.md](./catalog/images.md): 通用图片资源与文件访问规则
- [catalog/movies.md](./catalog/movies.md): 影片目录、详情、订阅和关联资源
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
- [discovery/hot-reviews.md](./discovery/hot-reviews.md): JavDB 热评快照分页查询
- [discovery/ranking-sources.md](./discovery/ranking-sources.md): 多来源排行榜资源与榜单条目查询

### Transfers

- [transfers/downloads.md](./transfers/downloads.md): 下载器配置与下载任务

### Releases

- [releases/2026-06-13-non-jav-videos-and-clips.md](./releases/2026-06-13-non-jav-videos-and-clips.md): 非 JAV 视频管理与视频片段收藏接口总览
- [releases/2026-05-07-actor-year-movie-count.md](./releases/2026-05-07-actor-year-movie-count.md): 女优影片年份数量返回

### Deployment

- [deployment/docker.md](./deployment/docker.md): Docker 部署教程
- [deployment/commands.md](./deployment/commands.md): 容器启动后的初始化、导入和单次任务命令
- [deployment/external-service-tests.md](./deployment/external-service-tests.md): 外部服务 `click` 测试命令说明

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
- `hot reviews`
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
