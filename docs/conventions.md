# API 设计约定

本文件是 SakuraMedia API 的统一设计约定。新增或修改接口时遵循本文；具体路径、参数和响应结构以代码中的路由、Schema 和生成的 OpenAPI 为准，不再维护逐接口文档。本文描述设计要求，不代表现有接口已全部完成对齐。

## 设计原则

- 面向业务资源设计接口，以清晰的资源边界和 HTTP 语义表达行为。
- 同类接口保持一致的命名、参数、响应和错误语义，避免让客户端为相同能力编写多套处理逻辑。
- 请求和响应通过 Schema 明确定义并校验，不直接暴露数据库模型、内部路径或敏感凭据。
- router 只负责参数接入、依赖与响应，业务编排放在 service，避免在接口层堆积复杂逻辑。
- 读取操作不改变业务状态；耗时操作建模为任务资源，返回任务标识供客户端查询进度和结果。
- 默认要求认证，普通接口通过统一依赖校验身份，媒体文件通过 URL 签名校验访问权限；校验通过后才能读取数据或执行操作。
- 只实现当前需求，不为未确定的场景增加抽象、兼容分支或额外接口。

## 路径与资源命名

- 路径只使用资源名词，不使用动作型片段，如 `add`、`remove`、`toggle`、`sub`、`unsub`、`info`、`list`、`all`
- 集合资源使用复数名词，如 `/movies`、`/actors`、`/playlists`
- 子资源使用嵌套路径，如 `/movies/{movie_number}/snapshots`
- 搜索、筛选、排序、分页优先放在查询参数中表达
- 需要会话状态的能力，建模为会话资源，如 `/image-search/sessions`

## HTTP 方法语义

- `GET`: 读取集合或资源详情
- `POST`: 创建资源或创建一次性会话
- `PUT`: 幂等设置某个状态
- `PATCH`: 局部更新资源
- `DELETE`: 删除资源或解除资源关系

## 系统配置类资源约定

- 系统配置类资源推荐使用 `GET + PATCH` 组合
- `GET` 返回当前生效配置快照
- `PATCH` 支持局部更新，未传字段保持原值
- 当配置需要触发耗时副作用时，使用查询参数显式控制即时应用，如 `apply_now=true|false`

## 状态码约定

- `200 OK`: 成功读取或成功更新并返回响应体
- `201 Created`: 成功创建资源
- `204 No Content`: 成功删除或成功执行无响应体的幂等操作
- `400 Bad Request`: 参数格式错误
- `401 Unauthorized`: 缺少有效认证
- `403 Forbidden`: 已认证但无权访问
- `404 Not Found`: 资源不存在
- `409 Conflict`: 资源状态冲突
- `422 Unprocessable Entity`: 字段通过 JSON 解析但业务校验失败

## 成功响应格式

成功响应不再使用统一包装对象。服务端直接返回资源对象、资源列表或分页对象。

单资源示例：

```json
{
  "movie_number": "ABC-001",
  "title": "Movie Title"
}
```

列表分页示例：

```json
{
  "items": [
    {
      "movie_number": "ABC-001",
      "title": "Movie Title"
    }
  ],
  "page": 1,
  "page_size": 20,
  "total": 1
}
```

## 错误响应格式

所有错误响应统一返回：

```json
{
  "error": {
    "code": "resource_not_found",
    "message": "Movie not found",
    "details": {
      "movie_number": "ABC-001"
    }
  }
}
```

字段说明：

- `code`: 稳定的程序化错误码
- `message`: 面向客户端的简洁描述
- `details`: 可选的补充上下文

## 分页、过滤与排序

- 偏移分页使用 `page` 与 `page_size`
- 游标分页只用于结果量大且有会话上下文的接口
- 关键词搜索使用 `query`
- 排序字段使用 `sort`
- 过滤条件使用清晰字段名，如 `year`、`tag_ids`、`status`
- 数组过滤值优先采用逗号分隔字符串，如 `tag_ids=1,2,3`

## 标识符约定

- 影片主标识为 `movie_number`
- 演员、系列、标签、播放列表、媒体、时间点使用稳定 ID
- 外部可公开的媒体标识为 `media_id`
- 不在路径中暴露特定实现细节，如历史加密 `aid`

## 字段命名

- JSON 字段统一使用 `snake_case`（`xx_xx`）
- 布尔字段使用 `is_xxx`、`has_xxx`、`can_xxx`
- 时间使用 ISO 8601 字符串，除非语义明确要求秒数或毫秒数
- 文件大小、偏移量、时长等数值字段使用整数

## 认证约定

- **除登录接口外，所有接口均需鉴权：普通接口使用 Bearer Token，媒体文件使用 URL 签名校验。**
- 登录接口使用账号密码换取令牌，无需预先提供 Bearer Token；入口为 `POST /auth/tokens`，`POST /auth/docs-token` 是 API 调试文档使用的同类登录入口。
- 除媒体文件访问外，其他接口统一使用 `Authorization: Bearer <access_token>`，通过共享的 `get_current_user` 依赖完成认证；令牌刷新、状态查询、媒体信息查询及插件管理接口均遵守此规则。
- 媒体文件访问是 Bearer Token 的例外：图片、视频播放流、字幕和片段文件通过 URL 中的 `expires + signature` 校验，无需额外携带 Bearer Token。统一复用 `src/common/file_signatures.py` 的签名与过期时间算法，缺少签名、签名无效或过期时返回 `403 Forbidden`。
- URL 签名仅授权访问对应媒体文件，不代表登录身份，不能用于调用其他业务接口。
- 使用 Bearer Token 的接口缺少令牌、令牌无效或过期时返回 `401 Unauthorized`；已认证但无权执行操作时返回 `403 Forbidden`。
- 不在 URL、日志或错误响应中泄露访问令牌、密码等敏感信息。

## 用户上下文约定

- 系统只支持一个账号，不在普通业务路径中显式暴露账号标识
- 订阅、播放列表、播放进度、媒体时间点等都以当前登录会话解释
- 文档中的账号态字段默认以当前账号视角解释，如 `is_subscribed`、`last_position_seconds`
- 账号维护通过 `/account` 资源完成

## 示例规范

- 示例优先展示推荐调用方式，而不是兼容旧实现
- 所有示例字段名必须与文档正文一致
- 示例响应必须体现真实状态码语义

## 非目标

- 本规范不追求 HATEOAS
- 本规范不提供旧接口迁移映射
- 本规范不以当前实现代码为约束
