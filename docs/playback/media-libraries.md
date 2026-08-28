# 媒体库

媒体库由 provider bundle 标识。宿主保存 `provider_key`、opaque
`provider_config` 和 provider 返回的 `account_key`，不解释配置内容。配置字段、敏感字段
掩码和更新时的 secret/read-only 合并均由 bundle 的 `ConfigField` 声明驱动。

## Provider 目录

```http
GET /media-libraries/providers
```

返回可用 provider 的 `provider_key`、显示名称、`library_config_fields` 和
`download_config_fields`。`download_config_fields` 为 `null` 表示该 provider 不支持下载，
为空数组表示支持下载但不需要额外配置。未安装的 provider 不会出现在目录中。

## 媒体库 API

```http
GET    /media-libraries
POST   /media-libraries
PATCH  /media-libraries/{library_id}
DELETE /media-libraries/{library_id}
```

创建请求：

```json
{
  "name": "主媒体库",
  "provider_key": "example",
  "provider_config": {"account": "opaque"}
}
```

创建和更新会调用 provider 的 `prepare_library`。宿主只检查配置 key 是否在
`library_config_fields` 中，并保留 provider 返回的配置；secret 字段不会在普通响应中
回显。provider 连接失败、鉴权失败和配置错误分别映射为结构化 `provider_*` 错误。

PATCH 不传 `provider_config` 时保留原配置；传对象时更新配置。显式传 `null` 返回 422。
更新对象中缺失的 secret 和 read-only 字段保留原值。

媒体库删除前必须先处理其媒体、下载客户端和其它外键引用；删除不会由宿主猜测或清理
provider 侧资源。
