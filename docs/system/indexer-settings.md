# Indexer Settings

## 资源说明

索引器配置资源用于读取和维护系统当前使用的 Torznab 索引器配置，**全部持久化在数据库**
（`Indexer` / `IndexerDownloadClient` 表），`config.toml` 不再承载任何索引器字段。

- 每个 `indexer` 是一条独立的 Torznab 端点：`url` 为搜索接口地址，`api_key` 为该端点
  可选的鉴权 key（为空则搜索请求不携带 `apikey` 参数，兼容免鉴权 Torznab 服务）
- `url` 只填接口地址，鉴权 key 统一放 `api_key` 字段；不要把 key 拼进 url——客户端会另外
  附加 `apikey` 参数，两个同名参数并存时服务端取哪个行为不确定
- 每个 `indexer` 至少绑定一个 `DownloadClient`，BT 索引器允许同时绑定多个（如 qBittorrent
  与 115 离线各一）；提交下载时按全局偏好 `[downloads].preferred_client_kinds` 从绑定集合中挑选，
  前端也可显式指定
- PT 索引器只能绑定 qBittorrent，不能绑定 115 离线下载入口
- `kind` 用于标记索引器种类，当前支持 `pt` 与 `bt`

## 资源模型

```json
{
  "indexers": [
    {
      "id": 1,
      "name": "mteam",
      "url": "http://host:port/api/v2.0/indexers/0magnet/results/torznab/",
      "kind": "pt",
      "api_key": "secret-key",
      "download_clients": [
        {"id": 2, "name": "qb-main", "kind": "qbittorrent"}
      ]
    }
  ]
}
```

`api_key` 明文返回（单账号体系、前端自律），未配置时为 `null`。

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/indexer-settings` | 读取当前索引器配置 |
| `PATCH` | `/indexer-settings` | 修改索引器配置 |
| `GET` | `/indexer-settings/test` | 测试 Torznab 连通性 |

## `GET /indexer-settings`

需要 Bearer Token。

成功响应：

- `200 OK`: 返回当前索引器配置

## `PATCH /indexer-settings`

需要 Bearer Token。

请求体支持局部更新；若传入 `indexers`，则整体替换当前数据库中的列表。`api_key` 可选，
传空串/空白会被归一为 `null`（不携带 apikey）。

请求体示例：

```json
{
  "indexers": [
    {
      "name": "mteam",
      "url": "http://host:port/api/v2.0/indexers/0magnet/results/torznab/",
      "kind": "pt",
      "api_key": "secret-key",
      "download_client_ids": [1]
    },
    {
      "name": "dmhy",
      "url": "https://example.com/torznab/",
      "kind": "bt",
      "api_key": null,
      "download_client_ids": [2, 3]
    }
  ]
}
```

错误语义：

- `empty_indexer_settings_update`: 未提供任何可更新字段
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

用数据库中已配置的 `indexers`（每个索引器使用自己的 `api_key`），对固定番号 `SSNI-888`
发起一次真实 Torznab 搜索，以此验证各端点地址与鉴权 key 整体可用。不接受请求体，
也不会写入任何远端状态。

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
    "type": "torznab_request_error",
    "message": "..."
  }
}
```

`error.type` 取值：

- `no_indexers_configured`: 数据库中尚未配置任何 indexer，未发起请求
- `torznab_request_error`: 请求 Torznab 端点失败（网络错误、apikey 无效、响应解析失败等），
  详情见 `message`

## 设计备注

- 客户端按 Torznab 协议搜索（`t=search&q=...&cat=6000`），不再绑定 Jackett/Prowlarr 特定语义；
  `api_key` 未配置时请求不带 `apikey` 参数，可直连免鉴权 Torznab 服务
- 每个索引器的 `api_key` 独立配置；从旧版全局 `config.toml` key 升级的用户需要在这里
  逐个重配一次，存量不会自动回填
- `kind` 用于标记索引器类型，当前支持 `pt` 与 `bt`
- PT 站的下载/做种规则要求使用 qBittorrent，因此禁止绑定 cloud115；BT 索引器不受此限制
- `indexer.name` 是下载候选自动解析目标下载器的匹配键
- 候选卡片上的 `resolved_client_*` 是按 `[downloads].preferred_client_kinds` 全局偏好从绑定集合
  预解析的默认下载器；偏好只影响挑选顺序，选中的下载器执行失败会直接报错、不自动降级
- 配置更新只写数据库，不涉及 `config.toml`，无需重启服务
