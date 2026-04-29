# Account

## 资源说明

账号资源负责读取和维护系统中的唯一账号资料。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

## 资源模型

```json
{
  "username": "account",
  "created_at": "2026-03-08T09:00:00",
  "last_login_at": "2026-03-08T10:00:00"
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/account` | 读取当前账号资料 |
| `PATCH` | `/account` | 修改账号用户名 |
| `POST` | `/account/password` | 修改账号密码 |

## `GET /account`

需要 Bearer Token。返回当前唯一账号信息。

## `PATCH /account`

需要 Bearer Token。

请求体：

```json
{
  "username": "renamed-account"
}
```

错误语义：

- `username_conflict`: 用户名冲突

## `POST /account/password`

需要 Bearer Token。

请求体：

```json
{
  "current_password": "password123",
  "new_password": "new-password123"
}
```

成功状态码：

- `204 No Content`

错误语义：

- `invalid_credentials`: 当前密码错误
