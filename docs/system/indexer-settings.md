# Indexer Settings

## 资源说明

索引器配置资源用于读取和维护系统当前使用的 Jackett 配置。

- `type` 与 `api_key` 持久化在 `config.toml`
- `indexers` 明细持久化在数据库
- 每个 `indexer` 必须绑定一个 `DownloadClient`

## 资源模型

```json
{
  "type": "jackett",
  "api_key": "secret-key",
  "indexers": [
    {
      "id": 1,
      "name": "mteam",
      "url": "http://host:port/api/v2.0/indexers/0magnet/results/torznab/",
      "kind": "pt",
      "download_client_id": 2,
      "download_client_name": "qb-main"
    }
  ]
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/indexer-settings` | 读取当前索引器配置 |
| `PATCH` | `/indexer-settings` | 修改索引器配置 |

## `GET /indexer-settings`

需要 Bearer Token。

成功响应：

- `200 OK`: 返回当前索引器配置

## `PATCH /indexer-settings`

需要 Bearer Token。

请求体支持局部更新；若传入 `indexers`，则整体替换当前数据库中的列表。

请求体示例：

```json
{
  "type": "jackett",
  "api_key": "updated-secret-key",
  "indexers": [
    {
      "name": "mteam",
      "url": "http://host:port/api/v2.0/indexers/0magnet/results/torznab/",
      "kind": "pt",
      "download_client_id": 1
    },
    {
      "name": "dmhy",
      "url": "https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
      "kind": "bt",
      "download_client_id": 2
    }
  ]
}
```

错误语义：

- `empty_indexer_settings_update`: 未提供任何可更新字段
- `invalid_indexer_settings_type`: 索引器类型为空或不支持
- `invalid_indexer_settings_api_key`: API key 为空
- `invalid_indexer_settings_indexers`: `indexers` 不是列表
- `invalid_indexer_settings_name`: indexer 名称为空
- `duplicate_indexer_settings_name`: indexer 名称重复
- `invalid_indexer_settings_url`: indexer URL 为空或不是合法的 `http/https` 地址
- `invalid_indexer_settings_kind`: indexer 标识为空或不支持
- `invalid_indexer_settings_download_client_id`: `download_client_id` 非法
- `indexer_settings_download_client_not_found`: `download_client_id` 不存在

## 设计备注

- 当前仅支持 `jackett`
- `kind` 用于标记索引器类型，当前支持 `pt` 与 `bt`
- `indexer.name` 是下载候选自动解析目标下载器的匹配键
- 配置更新成功后会立即刷新当前进程内存配置，无需重启服务
