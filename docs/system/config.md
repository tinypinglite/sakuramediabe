# 统一配置 API

## 资源说明

统一配置接口用于**一次性读取和局部修改**全部 `config.toml` 配置项，替代「每个配置节各造一套接口」的重复模式。

- 所有配置节明文读写，敏感字段（密钥、密码、`database.url`、各 `api_key`）**不脱敏**——接口在已鉴权的单账号体系内，前端自律。
- 修改采用**嵌套 dict partial** 语义：只更新请求体里显式给出的字段，其余保持不变。
- 修改经由 `Settings` 模型校验（类型 + cron/URL 语义），通过后**落盘 toml 并热更新 api 进程内存**（复用 `update_settings`）。
- 每个配置项带**生效方式**标注，接口如实告知「即时生效 / 需重启」，不制造「改了以为生效」的假象。

与现有 `/indexer-settings`（承载 DB `Indexer` 表明细）与 `/movie-desc-translation-settings/test`（翻译连通性探测）**并存**：那些能力不属于纯 toml 字段读写，统一配置接口不接管。`media.others_number_features`（原 `/collection-number-features`）的规范化已下沉到 `Media` 模型层，改动统一走 `PATCH /config`。

**本 API 不接管的键**：以下顶层键既不出现在 `GET /config` 的响应中，`PATCH /config` 收到也会直接返回 `readonly_config_key`：

- **`auth` 节整体**：
  - `auth.username` / `auth.password` 只在 `initdb` 建种子账号时读一次，运行时账号存 DB `User` 表，请走 [`/account`](./account.md) 接口。
  - `auth.secret_key` / `auth.file_signature_secret` 由 `ensure_runtime_config()` 首启自举生成随机值，运行时修改会立即作废所有 access token 或已下发的签名 URL，不该经通用配置接口暴露/修改。
  - `auth.algorithm` / `access_token_expire_minutes` / `refresh_token_expire_minutes` 属边缘可配，需要时直接改 `/data/config/config.toml` 并重启 api。
- **`enable_docs` 顶层字段**：字段仍在 `Settings` 里（默认 `False` = 关闭 Swagger/ReDoc），排障时可通过手动改 `/data/config/config.toml` 或环境变量打开并重启 api 进程生效，通过接口不暴露也不修改，避免把开发调试开关混入用户可改的配置集合。
- **`plugins` 节**：包含插件根目录（`root_dir`，默认 `/data/plugins`）、可信插件启用清单、任务 cron 覆盖及插件私有配置。API 与 APS 都在 import 阶段加载插件，且私有配置可能含凭据，因此本接口既不返回也不修改该节；安装/启停/升级/卸载统一走专用插件管理接口 `/system/plugins` 或 `plugins` CLI。具体契约见 [插件机制](../development/plugins.md)。

## 生效方式（三档）

系统为**双进程部署**（`api` 与 `aps` 调度进程各持一份 `settings` 快照），配置写只发生在 api 进程，故生效方式分三档：

| 档位 | 含义 |
|---|---|
| `hot` | api 进程内即时生效（每次使用现读 settings，或依赖缓存已在刷新时清理） |
| `restart_api` | 需重启 api 进程（连接池、日志、文档等在启动期读取一次） |
| `restart_scheduler` | 需重启 aps 调度进程（cron 装配时烘进 CronTrigger，或由定时任务消费） |

节级默认：

| 配置节 | 生效方式 |
|---|---|
| `media` `movie_info_translation` `metadata` `media_import` `image_search` `qdrant` | `hot` |
| `database` `logging` | `restart_api` |
| `scheduler` `downloads` | `restart_scheduler` |

> 注：`hot` 节中被定时任务消费的部分（如翻译、图搜、下载清理），其**定时执行路径**实为「需重启 aps」，只有 api 进程内的交互 / 手动触发才真正即时。

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/config` | 读取全部配置（明文）与每项生效方式 |
| `PATCH` | `/config` | 局部修改配置（嵌套 dict partial） |

## `GET /config`

需要 Bearer Token。返回全部配置节的明文快照与生效方式映射。

响应：

```json
{
  "values": {
    "database": { "engine": "postgres", "url": "postgresql://..." },
    "metadata": { "proxy": null, "javdb_host": "..." },
    "scheduler": { "movie_heat_cron": "15 0 * * *" }
  },
  "effects": {
    "database": "restart_api",
    "metadata": "hot",
    "scheduler": "restart_scheduler"
  }
}
```

- `values` 覆盖除只读键（`auth` 节 / `enable_docs` / `plugins`）外的全部配置节（见上文「本 API 不接管的键」）。
- `effects` 只列出可修改的节，前端可据此提示用户「改后是否需要重启」。

## `PATCH /config`

需要 Bearer Token。请求体为**嵌套 dict**，顶层键为配置节名（或顶层标量字段名），子键为节内字段名。仅更新显式给出的字段。

请求：

```json
{
  "metadata": { "proxy": "socks5://127.0.0.1:1080" },
  "scheduler": { "movie_heat_cron": "0 6 * * *" }
}
```

响应：

```json
{
  "values": { "...": "更新后的全部配置" },
  "applied": ["metadata.proxy"],
  "pending_restart": [
    { "field": "scheduler.movie_heat_cron", "restart": "scheduler" }
  ]
}
```

- `applied`：本次已即时生效（`hot`）的字段。
- `pending_restart`：需重启才生效的字段，`restart` 取 `api` 或 `scheduler`。
- 校验通过后配置整体落盘并热更新；非法值不会落盘。

### 校验与错误

| 错误码 | HTTP | 触发条件 |
|---|---|---|
| `empty_config_update` | 422 | 请求体为空对象 |
| `readonly_config_key` | 422 | 修改了本接口不接管的顶层键（当前是 `auth` 节 / `enable_docs` / `plugins`），`details.field` 指出具体键名；插件配置请走 `/system/plugins` |
| `unknown_config_field` | 422 | 顶层节或节内字段不存在（如拼错字段名），`details.field` 指出具体键 |
| `invalid_config_value` | 422 | 类型不符、子节不是对象、cron 表达式非法、URL 格式非法等 |

- **cron**：`scheduler.*_cron` 必须能被 APScheduler 解析，否则拒绝——避免非法 cron 落盘后拖垮 aps 进程重启。
- **URL**：`movie_info_translation.base_url`、`qdrant.url`、`image_search.inference_base_url`、`metadata.gfriends_*_url` 必须是 http/https；`metadata.proxy` 允许 http/https/socks5(h)。

这些语义校验分档执行：
- **启动加载**（`Settings()` 从 toml 加载）为宽松档，非法值仅打 warning、保留原值，避免存量非法配置让进程启动即崩。
- **本接口写入**（`Settings.model_validate(..., context={"strict": True})`）为严格档，非法值直接 422，阻止落盘。

## 注意事项

- `database.url` 明文包含数据库密码，随 `GET /config` 返回，请确保接口经鉴权且走 HTTPS。
