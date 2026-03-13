# Authentication Tokens

## 资源说明

认证资源负责用唯一账号的用户名和密码换取访问令牌。客户端后续通过 Bearer Token 调用受保护接口。

## 资源模型

```json
{
  "access_token": "jwt-token",
  "refresh_token": "refresh-token",
  "token_type": "Bearer",
  "expires_in": 3600,
  "expires_at": "2026-03-08T10:00:00Z",
  "refresh_expires_at": "2026-03-15T10:00:00Z",
  "user": {
    "username": "account"
  }
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/auth/tokens` | 使用用户名和密码创建访问令牌 |
| `POST` | `/auth/token-refreshes` | 使用 refresh token 轮换一组新令牌 |

## `POST /auth/tokens`

使用唯一账号的用户名和密码创建访问令牌。

鉴权：无需 Bearer Token（登录接口例外）。

请求体：

```json
{
  "username": "account",
  "password": "password123"
}
```

成功响应：

```json
{
  "access_token": "jwt-token",
  "refresh_token": "refresh-token",
  "token_type": "Bearer",
  "expires_in": 3600,
  "expires_at": "2026-03-08T10:00:00Z",
  "refresh_expires_at": "2026-03-15T10:00:00Z",
  "user": {
    "username": "account"
  }
}
```

错误语义：

- `invalid_credentials`: 用户名或密码错误

## `POST /auth/token-refreshes`

使用一个有效的 refresh token 轮换出一组新的访问令牌和 refresh token。

鉴权：需要 Bearer Token。

请求体：

```json
{
  "refresh_token": "refresh-token"
}
```

错误语义：

- `invalid_refresh_token`: refresh token 无效、过期或已撤销

## 设计备注

- 系统只支持一个账号，但仍保留 access token 和 refresh token
- refresh token 采用轮换策略，每次刷新都会返回新的 refresh token
- access token 只需要标识当前已登录账号，不再携带角色或账号状态
