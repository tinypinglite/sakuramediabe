# SakuraMedia API 设计文档

本目录描述 SakuraMedia 服务端的目标 API 设计。

## 全局设计原则

- 通用规范见 [conventions.md](./conventions.md)
- 除登录接口外，所有接口默认要求 `Authorization: Bearer <token>`
- 所有错误响应默认返回统一的 `error` 对象

## 文档导航

### System

- [system/auth.md](./system/auth.md): 登录与访问令牌
- [system/account.md](./system/account.md): 唯一账号资料与密码维护

### Catalog

- [catalog/images.md](./catalog/images.md): 通用图片资源与文件访问规则
- [catalog/movies.md](./catalog/movies.md): 影片目录、详情、订阅和关联资源
- [catalog/actors.md](./catalog/actors.md): 演员目录、订阅和关联资源
- [catalog/tags.md](./catalog/tags.md): 标签目录与标签下影片

### Collections

- [collections/playlists.md](./collections/playlists.md): 播放列表与影片归档

### Playback

- [playback/media.md](./playback/media.md): 媒体资源、播放流、缩略图、进度和精彩时间点
- [playback/media-libraries.md](./playback/media-libraries.md): 媒体库配置管理

### Discovery

- [discovery/image-search.md](./discovery/image-search.md): 以图搜图会话与结果分页

### Transfers

- [transfers/downloads.md](./transfers/downloads.md): 下载器配置与下载任务

### Deployment

- [deployment/docker.md](./deployment/docker.md): Docker 本地构建与单容器部署
- [deployment/commands.md](./deployment/commands.md): 容器启动后的初始化、导入和单次任务命令

## 资源清单

- `auth tokens`
- `account`
- `movies`
- `images`
- `actors`
- `tags`
- `playlists`
- `media`
- `media libraries`
- `media points`
- `image search sessions`
- `download clients`
- `download tasks`

## 通用认证说明

- 除登录接口和媒体资源(图片、视频、字幕) 外，所有接口都需要 Bearer Token
- 系统只支持一个账号
- 需要登录的业务数据以当前登录会话解释，不再按账号标识分区
