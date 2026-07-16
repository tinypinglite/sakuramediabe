# Indexer Settings

## 资源说明

索引器配置资源用于读取和维护系统当前使用的 Jackett 配置。

- `type` 与 `api_key` 持久化在 `config.toml`
- `indexers` 明细持久化在数据库
- 每个 `indexer` 至少绑定一个 `DownloadClient`，BT 索引器允许同时绑定多个（如 qBittorrent 与 115 离线各一）；
  提交下载时按全局偏好 `[downloads].preferred_client_kinds` 从绑定集合中挑选，前端也可显式指定
- PT 索引器只能绑定 qBittorrent，不能绑定 115 离线下载入口

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
      "download_clients": [
        {"id": 2, "name": "qb-main", "kind": "qbittorrent"}
      ]
    }
  ]
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/indexer-settings` | 读取当前索引器配置 |
| `PATCH` | `/indexer-settings` | 修改索引器配置 |
| `GET` | `/indexer-settings/test` | 测试 Jackett 连通性 |

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
      "download_client_ids": [1]
    },
    {
      "name": "dmhy",
      "url": "https://example.com/api/v2.0/indexers/dmhy/results/torznab/",
      "kind": "bt",
      "download_client_ids": [2, 3]
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
- `invalid_indexer_settings_download_client_ids`: `download_client_ids` 为空或含非法值
- `duplicate_indexer_settings_download_client_id`: `download_client_ids` 含重复 id
- `indexer_settings_download_client_not_found`: 绑定的下载客户端不存在
- `pt_indexer_cloud115_binding_unsupported`: PT 索引器尝试绑定 115 下载入口

## `GET /indexer-settings/test`

需要 Bearer Token。

用当前已保存的 `api_key` 与数据库中已配置的 `indexers`，对固定番号 `SSNI-888` 发起一次真实 Torznab 搜索，以此验证 Jackett 是否可达、`api_key` 是否有效、indexer 地址是否配置正确。不接受请求体，也不会写入任何远端状态。

成功响应：

- `200 OK`: 始终返回 `200`，探测结果体现在响应体的 `healthy` 字段中

响应体示例（健康）：

```json
{
  "healthy": true,
  "checked_at": "2026-07-11T08:00:00",
  "query": "SSNI-888",
  "indexers_checked": 2,
  "result_count": 5,
  "elapsed_ms": 342,
  "error": null
}
```

响应体示例（不健康）：

```json
{
  "healthy": false,
  "checked_at": "2026-07-11T08:00:00",
  "query": "SSNI-888",
  "indexers_checked": 2,
  "result_count": 0,
  "elapsed_ms": 30,
  "error": {
    "type": "jackett_request_error",
    "message": "..."
  }
}
```

`error.type` 取值：

- `no_indexers_configured`: 数据库中尚未配置任何 indexer，未发起请求
- `jackett_request_error`: 请求 Jackett 失败（网络错误、apikey 无效、响应解析失败等），详情见 `message`

## 设计备注

- 当前仅支持 `jackett`
- `kind` 用于标记索引器类型，当前支持 `pt` 与 `bt`
- PT 站的下载/做种规则要求使用 qBittorrent，因此禁止绑定 cloud115；BT 索引器不受此限制
- `indexer.name` 是下载候选自动解析目标下载器的匹配键
- 候选卡片上的 `resolved_client_*` 是按 `[downloads].preferred_client_kinds` 全局偏好从绑定集合预解析的默认下载器；偏好只影响挑选顺序，选中的下载器执行失败会直接报错、不自动降级
- 配置更新成功后会立即刷新当前进程内存配置，无需重启服务
