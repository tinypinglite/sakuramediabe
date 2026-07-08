# Movie Info Translation Settings（连通性探测）

## 资源说明

影片信息翻译配置由影片简介翻译和影片标题翻译共用，字段持久化在 `config.toml` 的 `[movie_info_translation]`。

- **读写走 [`/config`](./config.md) 统一配置接口**：`GET /config` 读取全部配置，`PATCH /config` 局部修改；专用的 GET/PATCH 端点已下线。
- 本路由现在只保留**连通性探测**端点：不落盘，用当前保存配置 + 请求体草稿覆盖发起一次真实翻译请求。
- 接口路径仍保持 `/movie-desc-translation-settings/test`，避免前端契约破坏；配置节名称已统一为 `movie_info_translation`。
- 探测**不检查** `movie_info_translation.enabled`。

## 端点

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/movie-desc-translation-settings/test` | 用当前配置 + 请求体草稿字段发起一次真实翻译，验证连通性 |

## `POST /movie-desc-translation-settings/test`

需要 Bearer Token。

请求体所有字段都是可选的：
- 传入字段：作为**草稿覆盖**当前保存配置对应项发起本次探测。
- 未传字段：回退到 `settings.movie_info_translation` 里当前保存值。

保存配置测试示例：

```json
{}
```

草稿直测示例：

```json
{
  "base_url": "http://127.0.0.1:8000",
  "api_key": "",
  "model": "gpt-4o-mini",
  "timeout_seconds": 180,
  "connect_timeout_seconds": 9,
  "text": "hi"
}
```

成功响应：

```json
{
  "ok": true
}
```

错误语义：

- 透传下游翻译客户端错误码，例如：
- `movie_desc_translation_unavailable`
- `movie_desc_translation_invalid_response`
- `movie_desc_translation_failed`
- `movie_desc_translation_empty_result`
- `invalid_movie_desc_translation_test_text`
- `movie_desc_translation_prompt_unavailable`
- `invalid_movie_desc_translation_base_url` / `invalid_movie_desc_translation_model` / `invalid_movie_desc_translation_timeout_seconds` / `invalid_movie_desc_translation_connect_timeout_seconds`：草稿字段本身不合法时先拦下

说明：

- `text` 不传时默认使用 `hi`
- 探测直接读取正式翻译任务使用的 prompt 文件，不接收请求体 prompt 覆盖
- 成功时只返回 `ok=true`，不回显测试文本和模型输出
- 接口不修改 `config.toml`，也不刷新全局运行时配置
