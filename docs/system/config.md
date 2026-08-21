# 统一配置 API

## 资源说明

统一配置接口用于**一次性读取和局部修改**全部 `config.toml` 配置项，替代「每个配置节各造一套接口」的重复模式。

- 所有配置节明文读写，敏感字段（密钥、密码、`database.url`、各 `api_key`）**不脱敏**——接口在已鉴权的单账号体系内，前端自律。
- 修改采用**嵌套 dict partial** 语义：只更新请求体里显式给出的字段，其余保持不变。
- 修改经由 `Settings` 模型校验（类型 + cron/URL 语义），通过后**原子写入 TOML**。
- 普通配置不会修改运行中进程的 `settings` 快照：写入后必须同时重启 API 与 APS，避免两个进程使用不一致配置。

与现有 `/indexer-settings`（承载 DB `Indexer` 表明细）**并存**：该能力不属于纯 toml 字段读写，统一配置接口不接管。`media.others_number_features`（原 `/collection-number-features`）的规范化已下沉到 `Media` 模型层，改动统一走 `PATCH /config`。

**本 API 不接管的键**：以下顶层键既不出现在 `GET /config` 的响应中，`PATCH /config` 收到也会直接返回 `readonly_config_key`：

- **`auth` 节整体**：
  - `auth.username` / `auth.password` 只在 `initdb` 建种子账号时读一次，运行时账号存 DB `User` 表，请走 [`/account`](./account.md) 接口。
  - `auth.secret_key` / `auth.file_signature_secret` 由 `ensure_runtime_config()` 首启自举生成随机值，运行时修改会立即作废所有 access token 或已下发的签名 URL，不该经通用配置接口暴露/修改。
  - `auth.algorithm` / `access_token_expire_minutes` / `refresh_token_expire_minutes` 属边缘可配，需要时直接改 `/data/config/config.toml` 并重启 api。
- **`enable_docs` 顶层字段**：字段仍在 `Settings` 里（默认 `False` = 关闭 Swagger/ReDoc），排障时可通过手动改 `/data/config/config.toml` 或环境变量打开并重启 api 进程生效，通过接口不暴露也不修改，避免把开发调试开关混入用户可改的配置集合。
- **`plugins` 节**：包含插件根目录（`root_dir`，默认 `/data/plugins`）、可信插件启用清单、任务 cron 覆盖及插件私有配置。API 与 APS 都在 import 阶段加载插件，且私有配置可能含凭据，因此本接口既不返回也不修改该节；安装/启停请走 `/system/plugins` 管理 API 或 `plugins` CLI，或直接编辑 `config.toml`。JavDB 登录账号等榜单凭据按插件放在 `plugins.settings.<plugin_id>` 下，不再属于 `metadata` 节。具体开发契约见 [插件系统开发指南](./plugins.md)。

## 生效方式

系统为**双进程部署**（`api` 与 `aps` 调度进程各持一份启动时的 `settings` 快照）。`PATCH /config` 只将已校验的完整配置原子写入 TOML，不更新 API 进程内存，也不清理依赖缓存。

每次局部更新都以当前已落盘的 TOML 为基准完成串行读改写，因此连续修改不同配置项不会相互覆盖。

因此，**每次普通配置更新都需要重启 API 和 APS 两个进程后才会生效**。更新响应固定返回 `restart_required: ["api", "aps"]`，客户端不应再按字段区分热更新或单进程重启。

`GET /config` 在重启前仍会返回当前进程加载的旧快照；`PATCH /config` 的 `values` 则是刚刚成功写入、将在下次启动时加载的新配置。

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/config` | 读取当前进程加载的全部配置（明文） |
| `PATCH` | `/config` | 局部修改配置（嵌套 dict partial） |

## `GET /config`

需要 Bearer Token。返回当前 API 进程加载的全部配置节明文快照。

响应：

```json
{
  "values": {
    "database": { "engine": "postgres", "url": "postgresql://..." },
    "metadata": { "javdb_host": "..." },
    "scheduler": { "movie_heat_cron": "15 0 * * *" }
  }
}
```

- `values` 覆盖除只读键（`auth` 节 / `enable_docs` / `plugins`）外的全部配置节（见上文「本 API 不接管的键」）。
- 若刚执行过 `PATCH /config` 但尚未重启，这里仍是旧快照；请以 PATCH 响应的 `values` 确认持久化结果。

## `PATCH /config`

需要 Bearer Token。请求体为**嵌套 dict**，顶层键为配置节名（或顶层标量字段名），子键为节内字段名。仅更新显式给出的字段。

请求：

```json
{
  "metadata": { "javdb_host": "jdforrepam.com" },
  "scheduler": { "movie_heat_cron": "0 6 * * *" }
}
```

响应：

```json
{
  "values": { "...": "更新后的全部配置" },
  "restart_required": ["api", "aps"]
}
```

- `values`：已成功写入 TOML、将在重启后加载的完整配置快照。
- `restart_required`：固定为 `api` 与 `aps`；两个进程均重启后，本次更新才会生效。
- 校验通过后配置整体原子落盘；非法值不会落盘，也不会影响当前运行中的配置。

### 校验与错误

| 错误码 | HTTP | 触发条件 |
|---|---|---|
| `empty_config_update` | 422 | 请求体为空对象 |
| `readonly_config_key` | 422 | 修改了本接口不接管的顶层键（当前是 `auth` 节 / `enable_docs` / `plugins`），`details.field` 指出具体键名；插件安装/启停请走 `/system/plugins` 或 `plugins` CLI |
| `unknown_config_field` | 422 | 顶层节或节内字段不存在（如拼错字段名），`details.field` 指出具体键 |
| `invalid_config_value` | 422 | 类型不符、子节不是对象、cron 表达式非法、URL 格式非法等 |

- **cron**：`scheduler.*_cron` 必须能被 APScheduler 解析，否则拒绝——避免非法 cron 落盘后拖垮 aps 进程重启。
- **下载进度快照**：`scheduler.download_progress_snapshot_interval_seconds` 控制 APS 宿主内部 qB 采样周期，默认 `5` 秒，允许 `1` 至 `60` 秒；修改后重启 APS 生效。该采样器不进入任务中心，也不会创建 TaskRun。
- **URL**：`qdrant.url`、`image_search.inference_base_url`、`metadata.gfriends_*_url` 必须是 http/https。

这些语义校验分档执行：
- **启动加载**（`Settings()` 从 toml 加载）为宽松档，非法值仅打 warning、保留原值，避免存量非法配置让进程启动即崩。
- **本接口写入**（`Settings.model_validate(..., context={"strict": True})`）为严格档，非法值直接 422，阻止落盘。

## 代理配置

外部站点请求**统一通过容器环境变量分流代理**，不再提供 config 层显式代理配置（`metadata.proxy` 已移除）。

### 环境变量 `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`（推荐容器部署用）

- 作用于全部 metadata 链路：JavDB API、JavDB 图片下载（jdbstatic.com 封面/剧照/头像）、GFriends filetree / 头像解析。
- 由部署方自行分流：设 `HTTP_PROXY` 指向代理软件（如 clash 混合端口）后，可在 `NO_PROXY` 排除直连域名，或交给代理软件自身的规则引擎分流，项目代码不做任何判断。`NO_PROXY` 遵循 curl 语义：`example.com`（不带点）排除该域自身及子域，`.example.com`（带点）只排除子域、不排除主域自身（如 `.jdbstatic.com` 不能排除 `jdbstatic.com` 本域）。
- 未设置环境变量时行为与以前完全一致（全部直连），老部署零感知。
- **存量迁移**：此前依赖 `metadata.proxy` 的部署，删除该配置后必须改用环境变量，否则原本走代理的 GFriends 请求会转为直连。

> 注意：qbittorrent / torznab / cloud115 等下载与网盘链路显式关闭了环境变量读取（`trust_env=False`），不受容器全局代理影响，保持直连。
>
> Linux 容器内访问宿主机代理端口需在 compose 加 `extra_hosts: "host.docker.internal:host-gateway"`，或直接用宿主机局域网 IP。

## 注意事项

- `database.url` 明文包含数据库密码，随 `GET /config` 返回，请确保接口经鉴权且走 HTTPS。
