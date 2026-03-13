# API 设计约定

本文件定义 SakuraMedia API 的统一约定。所有资源文档都应遵循这里的规则。

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

- 除登录接口外，所有接口都要求 Bearer Token
- 登录接口定义在 `auth` 资源下（如 `/auth/tokens`）
- 偏离默认规则时，必须在资源文档中单独注明

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
