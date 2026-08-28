# 系统配置

## 配置 API

```http
GET   /config
PATCH /config
```

PATCH 接受嵌套对象并按配置节合并。未知字段会被拒绝；写盘成功后返回
`restart_required`，提示需要重启的进程。敏感配置只应通过受控的部署配置注入，不能把
凭据写入日志或提交到仓库。

## 主要配置节

- `database`：PostgreSQL 连接参数。
- `auth`：单账号认证和签名密钥。
- `media`：媒体图片、缩略图和片段目录。
- `metadata`：JavDB、GFriends 等元数据 provider 配置。
- `plugins`：插件启用状态、插件设置和任务 cron 覆盖。
- `scheduler`：调度开关、日志目录和任务 cron。
- `downloads`：订阅自动下载的搜索新鲜度与重试上限。
- `image_search`、`qdrant`：图像检索服务和向量库配置。

媒体库和下载客户端的 `provider_config` 不在这里拼装。宿主只按 bundle 声明的
`ConfigField` 做字段白名单、secret 掩码和更新时的 secret/read-only 合并，其余内容由
provider 解释。
