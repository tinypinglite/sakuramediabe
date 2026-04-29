# Media Libraries

## 资源说明

媒体库资源用于管理本地媒体库存储配置。当前只提供列表、新增、修改、删除四个基础能力。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

## 资源模型

```json
{
  "id": 1,
  "name": "Main Library",
  "root_path": "/media/library/main",
  "created_at": "2026-03-08T09:30:00",
  "updated_at": "2026-03-08T09:30:00"
}
```

## 标识符说明

- `id`: 媒体库主标识，路径中唯一使用的媒体库标识
- `name`: 媒体库名称，要求全局唯一，但不作为路径标识

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/media-libraries` | 获取媒体库列表 |
| `POST` | `/media-libraries` | 创建媒体库 |
| `PATCH` | `/media-libraries/{library_id}` | 更新媒体库名称和路径 |
| `DELETE` | `/media-libraries/{library_id}` | 删除媒体库 |

## 详细接口定义

### Endpoint

`GET /media-libraries`

### Purpose

返回全部媒体库列表。不提供分页和筛选。

### Auth

需要 Bearer Token。

### Path Params

无。

### Query Params

无。

### Request Body

无。

### Success Responses

- `200 OK`: 返回媒体库数组

### Error Responses

- `401 Unauthorized`: 未认证

### Example Request

```http
GET /media-libraries
Authorization: Bearer <token>
```

### Example Response

```json
[
  {
    "id": 1,
    "name": "Main Library",
    "root_path": "/media/library/main",
    "created_at": "2026-03-08T09:30:00",
    "updated_at": "2026-03-08T09:30:00"
  }
]
```

### Endpoint

`POST /media-libraries`

### Purpose

创建一个新的媒体库。

### Auth

需要 Bearer Token。

### Path Params

无。

### Query Params

无。

### Request Body

```json
{
  "name": "Main Library",
  "root_path": "/media/library/main"
}
```

### Success Responses

- `201 Created`: 返回新建后的媒体库资源

### Error Responses

- `401 Unauthorized`: 未认证
- `409 Conflict`: `name` 或 `root_path` 已被占用
- `422 Unprocessable Entity`: 字段为空或 `root_path` 不是绝对路径

### Example Request

```http
POST /media-libraries
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "Main Library",
  "root_path": "/media/library/main"
}
```

### Example Response

```json
{
  "id": 1,
  "name": "Main Library",
  "root_path": "/media/library/main",
  "created_at": "2026-03-08T09:30:00",
  "updated_at": "2026-03-08T09:30:00"
}
```

### Endpoint

`PATCH /media-libraries/{library_id}`

### Purpose

修改媒体库名称和或路径。

### Auth

需要 Bearer Token。

### Path Params

- `library_id`: 媒体库 ID

### Query Params

无。

### Request Body

至少提供一个字段：`name`、`root_path`

```json
{
  "name": "Archive Library",
  "root_path": "/media/library/archive"
}
```

### Success Responses

- `200 OK`: 返回更新后的媒体库资源

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体库不存在
- `409 Conflict`: 新 `name` 或新 `root_path` 已被其他媒体库占用
- `422 Unprocessable Entity`: 请求体没有可更新字段、字段为空，或 `root_path` 不是绝对路径

### Example Request

```http
PATCH /media-libraries/1
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "Archive Library",
  "root_path": "/media/library/archive"
}
```

### Example Response

```json
{
  "id": 1,
  "name": "Archive Library",
  "root_path": "/media/library/archive",
  "created_at": "2026-03-08T09:30:00",
  "updated_at": "2026-03-09T10:00:00"
}
```

### Endpoint

`DELETE /media-libraries/{library_id}`

### Purpose

删除指定媒体库。

### Auth

需要 Bearer Token。

### Path Params

- `library_id`: 媒体库 ID

### Query Params

无。

### Request Body

无。

### Success Responses

- `204 No Content`: 删除成功

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体库不存在
- `409 Conflict`: 媒体库仍被业务数据引用，无法删除

### Example Request

```http
DELETE /media-libraries/1
Authorization: Bearer <token>
```

## 设计备注

- `MediaLibrary` 是受保护的系统配置资源，所有接口都要求 Bearer Token
- 当前只提供列表、新增、修改、删除，不扩展其他 playback 子资源
- 修改接口允许更新 `name` 与 `root_path`
- 删除是否允许取决于该媒体库是否仍被其他业务数据引用
