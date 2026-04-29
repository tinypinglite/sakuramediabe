# Metadata Provider License

## 资源说明

闭源元数据 Provider 授权接口用于读取本地授权状态，并通过一次性激活码激活 DMM、JavDB、MissAV 等闭源元数据能力。

- 激活码只通过接口请求体提交，不写入 `config.toml`、环境变量或日志。
- 授权状态文件写入 `/data/config/provider-license-state.json`，随 `/data/config` 挂载持久化。
- 授权中心代理配置在 `[metadata].license_proxy`，只影响授权中心请求，不影响 `[metadata].proxy` 的站点代理策略。

## 资源模型

```json
{
  "configured": true,
  "active": false,
  "instance_id": "inst_xxx",
  "expires_at": null,
  "license_valid_until": null,
  "renew_after_seconds": null,
  "error_code": "license_required",
  "message": "License activation is required"
}
```

字段说明：

- `configured`: 本地授权客户端是否已初始化。
- `active`: 当前授权租约是否可用。
- `instance_id`: 本地实例 ID，用于授权绑定排查。
- `expires_at`: 当前本地租约过期时间，Unix 秒；未激活时为 `null`。
- `license_valid_until`: 授权本身到期时间，Unix 秒；`null` 表示永久授权或未激活。
- `renew_after_seconds`: 建议续租间隔秒数；未激活时为 `null`。
- `error_code`: 授权不可用时的错误码。
- `message`: 授权不可用时的可读说明。

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/metadata-provider-license/status` | 读取闭源 Provider 授权状态 |
| `GET` | `/metadata-provider-license/connectivity-test` | 测试后端到授权中心的网络与代理可达性 |
| `POST` | `/metadata-provider-license/activate` | 使用激活码激活闭源 Provider 授权 |
| `POST` | `/metadata-provider-license/renew` | 手动续租闭源 Provider 授权租约 |

## `GET /metadata-provider-license/status`

需要 Bearer Token。

成功响应：

- `200 OK`: 始终返回授权状态；未激活、过期或本地状态不可用通过 `active=false`、`error_code` 和 `message` 表达。

## `GET /metadata-provider-license/connectivity-test`

需要 Bearer Token。

该接口直接请求授权中心 `https://sakuramedia-license-worker.tinyping.workers.dev/`，用于排查后端到授权中心的网络和 `[metadata].license_proxy` 配置是否可用。接口不会读取或修改本地授权状态，也不会触发激活或续租。

成功响应：

- `200 OK`: 始终返回测试结果；网络、代理、TLS 或超时异常通过 `ok=false` 与 `error` 字段表达。

示例响应：

```json
{
  "ok": true,
  "url": "https://sakuramedia-license-worker.tinyping.workers.dev/",
  "proxy_enabled": true,
  "elapsed_ms": 128,
  "status_code": 200,
  "error": null
}
```

## `POST /metadata-provider-license/activate`

需要 Bearer Token。

请求体：

```json
{
  "activation_code": "SMB-XXXX-XXXX-XXXX"
}
```

成功响应：

- `200 OK`: 返回激活后的授权状态资源。

错误语义：

- `invalid_request`: 激活码为空或请求参数不合法。
- `activation_code_invalid`: 激活码不存在或格式不可用。
- `activation_code_disabled`: 激活码已禁用。
- `activation_code_expired`: 激活码已过期。
- `activation_code_used`: 激活码已被使用且不能复用。
- `activation_conflict`: 并发激活冲突，可稍后重试。
- `too_many_requests`: 授权中心限流。
- `license_revoked` / `license_expired`: 授权已吊销或过期。
- `instance_disabled` / `instance_deactivated`: 当前实例不可用。
- `instance_mismatch` / `fingerprint_mismatch`: 当前设备与授权状态不匹配。
- `version_blocked`: 当前后端版本不允许激活。
- `license_unavailable`: 授权中心网络、代理或本地授权状态不可用。
- `license_server_error` / `license_create_failed`: 授权中心返回异常。

错误响应仍沿用全局格式：

```json
{
  "error": {
    "code": "activation_code_invalid",
    "message": "Activation code is invalid",
    "details": {
      "license_error_code": "activation_code_invalid"
    }
  }
}
```

## `POST /metadata-provider-license/renew`

需要 Bearer Token。

该接口手动请求授权中心续租当前本地授权租约，不需要提交激活码，也不会修改 `config.toml`。

成功响应：

- `200 OK`: 返回续租后的授权状态资源。

错误语义：

- `too_many_requests`: 授权中心限流。
- `license_revoked` / `license_expired`: 授权已吊销或过期。
- `instance_disabled` / `instance_deactivated`: 当前实例不可用。
- `instance_mismatch` / `fingerprint_mismatch`: 当前设备与授权状态不匹配。
- `version_blocked`: 当前后端版本不允许续租。
- `request_replayed` / `request_timestamp_invalid`: 续租请求时间戳或重放校验失败。
- `license_unavailable`: 授权中心网络、代理或本地授权状态不可用。
- `license_server_error`: 授权中心返回异常。

错误响应仍沿用全局格式，`details` 只透出授权错误码：

```json
{
  "error": {
    "code": "license_revoked",
    "message": "License is revoked",
    "details": {
      "license_error_code": "license_revoked"
    }
  }
}
```
