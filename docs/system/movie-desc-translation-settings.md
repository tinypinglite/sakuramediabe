# Movie Info Translation Settings

## 资源说明

影片信息翻译配置资源用于读取、维护和测试当前大模型翻译服务配置。
当前这份配置由影片简介翻译和影片标题翻译共用。

- 所有配置字段都持久化在 `config.toml` 的 `[movie_info_translation]`
- `test` 接口只做外部服务连通性和返回结构验证，不会落盘
- `test` 接口不会检查 `movie_info_translation.enabled`
- 接口路径仍保持 `/movie-desc-translation-settings`，仅配置项名称已统一为 `movie_info_translation`

## 资源模型

```json
{
  "enabled": false,
  "base_url": "http://llm.internal:8000",
  "api_key": "secret-token",
  "model": "gpt-4o-mini",
  "timeout_seconds": 300.0,
  "connect_timeout_seconds": 3.0
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/movie-desc-translation-settings` | 读取当前共享翻译配置 |
| `PATCH` | `/movie-desc-translation-settings` | 修改当前共享翻译配置 |
| `POST` | `/movie-desc-translation-settings/test` | 测试已保存配置或草稿配置 |

## `GET /movie-desc-translation-settings`

需要 Bearer Token。

成功响应：

- `200 OK`: 返回当前运行时配置快照

## `PATCH /movie-desc-translation-settings`

需要 Bearer Token。

请求体支持局部更新。

请求体示例：

```json
{
  "enabled": true,
  "base_url": "http://llm.internal:8000",
  "api_key": "",
  "model": "gpt-4o-mini",
  "timeout_seconds": 120,
  "connect_timeout_seconds": 5
}
```

错误语义：

- `empty_movie_desc_translation_settings_update`: 未提供任何可更新字段
- `invalid_movie_desc_translation_enabled`: `enabled` 为空
- `invalid_movie_desc_translation_base_url`: `base_url` 为空或不是合法的 `http/https` 地址
- `invalid_movie_desc_translation_api_key`: `api_key` 不是字符串
- `invalid_movie_desc_translation_model`: `model` 为空
- `invalid_movie_desc_translation_timeout_seconds`: `timeout_seconds` 不是正数
- `invalid_movie_desc_translation_connect_timeout_seconds`: `connect_timeout_seconds` 不是正数

## `POST /movie-desc-translation-settings/test`

需要 Bearer Token。

该接口支持两种使用方式：

- 不传任何覆盖字段：直接测试当前已保存配置
- 传入部分或全部覆盖字段：按“当前配置 + 草稿覆盖”发起一次性测试

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

成功响应示例：

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

说明：

- `text` 不传时默认使用 `hi`
- 测试接口会直接读取正式翻译任务使用的 prompt 文件，而不是接收请求体 prompt
- 成功时只返回 `ok=true`，不回显测试文本和模型输出
- 接口不会修改 `config.toml`，也不会刷新全局运行时配置
