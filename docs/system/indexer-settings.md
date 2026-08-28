# Indexer 设置

Indexer 保存 Torznab 搜索地址、索引器类型和可选鉴权 key，并按绑定顺序关联下载客户
端。下载客户端的 provider 配置属于客户端资源，不复制到 Indexer 设置。

## API

```http
GET   /indexer-settings
PATCH /indexer-settings
GET   /indexer-settings/test
```

更新请求的 `indexers` 是整表替换：

```json
{
  "indexers": [
    {
      "name": "主索引器",
      "url": "https://indexer.example/api",
      "kind": "bt",
      "api_key": "optional-key",
      "download_client_ids": [1]
    }
  ]
}
```

`kind` 当前为 `pt` 或 `bt`。每个索引器至少绑定一个已存在的下载客户端，ID 不得重复；
客户端响应只包含 id 和名称。省略已有索引器的 `api_key` 会保留原值，显式传空值会清除。

## 连通性测试

`GET /indexer-settings/test` 使用固定番号对已配置索引器执行一次搜索，返回检查数量、
候选数量和耗时。无索引器或搜索失败时 `healthy=false`，错误详情不会包含密钥。
