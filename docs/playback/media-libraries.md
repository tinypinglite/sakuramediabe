# Media Libraries

## 资源说明

媒体库 = **一个存储后端 + 它的连接配置**（`backend` + `backend_config`）：

- `local`：本地目录库，`backend_config = {"root_path": "<绝对路径>"}`
- `cloud115`：115 网盘库，内部 `backend_config = {"cookies": "...", "root_cid": "...", "app": "..."}`；API 响应只公开 `root_cid/app`，不会返回 cookies；
  `root_cid` 是 115 根目录下系统管理的 `sakuramedia/` 目录（创建库时 find-or-create），
  其下 `jav/` 子树由导入管线维护。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

## 资源模型

```json
{
  "id": 1,
  "name": "Main Library",
  "backend": "local",
  "backend_config": {"root_path": "/media/library/main"},
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
| `GET` | `/media-libraries` | 获取媒体库列表（含全部 backend） |
| `POST` | `/media-libraries` | 创建媒体库（仅放行 `backend=local`） |
| `PATCH` | `/media-libraries/{library_id}` | 更新媒体库名称 |
| `DELETE` | `/media-libraries/{library_id}` | 删除媒体库 |
| `POST` | `/media-libraries/cloud115/qrlogin/token` | 建 115 扫码会话（返回二维码 PNG base64） |
| `POST` | `/media-libraries/cloud115/qrlogin/status` | 长轮询扫码状态（阻塞 ~30s） |
| `POST` | `/media-libraries/cloud115` | 扫码确认后创建 cloud115 媒体库 |
| `POST` | `/media-libraries/cloud115/{library_id}/reauth` | 为已有 cloud115 库重新扫码更新 cookies |
| `GET` | `/media-libraries/cloud115/{library_id}/entries` | 浏览该库账号的 115 目录（选导入源用） |

## cloud115 媒体库

### 创建（扫码登录三步）

1. `POST /media-libraries/cloud115/qrlogin/token` → `{uid, time, sign, qrcode_png_base64}`，前端拼 data URL 展示二维码。
2. `POST /media-libraries/cloud115/qrlogin/status`（body 透传 `uid/time/sign`）长轮询：`waiting` / `scanned` 继续轮询，`confirmed` 进第 3 步，`expired` / `canceled` 终止重来。
3. `POST /media-libraries/cloud115`，body `{"name": "...", "uid": "...", "app": "alipaymini"}`：换 cookies → 探活校验 → find-or-create `sakuramedia/` → 落库。`app` 是 115 登录槽（默认 `alipaymini` 支付宝小程序端，不挤掉用户手机/网页登录）。同一 115 用户 ID 只能绑定一个媒体库，重复绑定返回 `409 cloud115_account_already_bound`。

### 已有库重新认证

cookies 明确失效后，重新执行扫码 token/status 两步；确认完成后调用
`POST /media-libraries/cloud115/{library_id}/reauth`，body 为
`{"uid": "...", "app": "alipaymini"}`。服务端会校验扫码结果必须仍是该库原先绑定的
115 用户 ID，只更新 `cookies/app`，保留 `root_cid`、Media 与导入作业；扫到其它账号返回
`409 cloud115_account_mismatch`。响应仍会脱敏，不返回 cookies。

### 目录浏览（导入源选择器）

`GET /media-libraries/cloud115/{library_id}/entries?cid=0&offset=0&limit=200`

- 列一页目录/文件条目（`entry_id/name/is_dir/size/is_video/mtime`），`cid=0` 为网盘根。
- 响应带该库的 `root_cid`：**前端目录选择器必须据此禁选管理目录及其子树**；服务端在触发导入时还会做互不包含校验兜底（见 `docs/transfers/media-import.md`）。
- 库不是 cloud115 返回 `422 media_library_backend_mismatch`；明确 cookies 失效返回 `422 cloud115_cookies_invalid`；登录探测遇到超时、5xx 或非法响应返回 `502 cloud115_upstream_error`，不会误导用户重新扫码；限流 `429`；目录不存在 `404 cloud115_dir_not_found`。

### 运行时行为

- **播放**：`GET /media/{id}/stream`（签名 URL 不变）对 cloud115 媒体现拿一条绑定请求方 UA 的 115 直链后 `302`；直链有 `t=` 过期，播放器 seek 重新请求 `/stream` 即重新拿链。
- **缩略图**：APS `generate-media-thumbnails` 对 cloud115 媒体走直链受控 Range 读抽帧（每 10s 一帧），并用首帧回填 `resolution`。
- **片段**：创建片段时现取绑定专用 UA 的 115 直链，经单请求、可 seek 的受控 RangeReader 交给 PyAV 按缩略图区间无转码 remux，生成本地独立片段资产；不会让 ffmpeg 并发直读 115 URL。
- **删除**：删除 cloud115 Media 会同时删 115 云端文件（进 115 回收站）；cookies 失效等上游错误时记录保留并报错，避免云端孤儿文件。
- **有效性对账**：APS `scan-media-files` 对 cloud115 媒体按 pickcode 探活——远端已删/封禁标 `invalid`、重新出现复活；cookies 失效等上游不可用**跳过本条不动 valid**。
- **cookies 保活**：APS `keepalive-cloud115-cookies`（默认每 20 分钟）探活并把 SDK merge 到的最新 cookies 快照回写 `backend_config`；探测结果分为 `alive/expired/unavailable`，仅明确 `expired` 时发系统通知（同题未读去重）。

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
    "backend": "local",
    "backend_config": {"root_path": "/media/library/main"},
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
  "backend": "local",
  "backend_config": {"root_path": "/media/library/main"}
}
```

> 本端点仅放行 `backend=local`；cloud115 库的创建走扫码流程（`POST /media-libraries/cloud115`，
> 见上文 [cloud115 媒体库](#cloud115-媒体库)），传其它 backend 返回 `422 unsupported_media_library_backend`。

### Success Responses

- `201 Created`: 返回新建后的媒体库资源

### Error Responses

- `401 Unauthorized`: 未认证
- `409 Conflict`: `name` 或 `root_path` 已被占用
- `422 Unprocessable Entity`: 字段为空、`root_path` 不是绝对路径，或 backend 不可经本端点创建

### Example Request

```http
POST /media-libraries
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "Main Library",
  "backend": "local",
  "backend_config": {"root_path": "/media/library/main"}
}
```

### Example Response

```json
{
  "id": 1,
  "name": "Main Library",
  "backend": "local",
  "backend_config": {"root_path": "/media/library/main"},
  "created_at": "2026-03-08T09:30:00",
  "updated_at": "2026-03-08T09:30:00"
}
```

### Endpoint

`PATCH /media-libraries/{library_id}`

### Purpose

修改媒体库名称（backend 与 backend_config 创建后不可经本端点修改；cloud115 的 cookies 由保活任务与重新扫码维护）。

### Auth

需要 Bearer Token。

### Path Params

- `library_id`: 媒体库 ID

### Query Params

无。

### Request Body

至少提供一个字段：`name`

```json
{
  "name": "Archive Library"
}
```

### Success Responses

- `200 OK`: 返回更新后的媒体库资源

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 媒体库不存在
- `409 Conflict`: 新 `name` 已被其他媒体库占用
- `422 Unprocessable Entity`: 请求体没有可更新字段或字段为空

### Example Request

```http
PATCH /media-libraries/1
Authorization: Bearer <token>
Content-Type: application/json

{
  "name": "Archive Library"
}
```

### Example Response

```json
{
  "id": 1,
  "name": "Archive Library",
  "backend": "local",
  "backend_config": {"root_path": "/media/library/main"},
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
- 一个媒体库要么本地、要么网盘，不混合；`backend` 是 Media 后端分派（播放/缩略图/对账/导入）的权威来源
- 修改接口只允许更新 `name`；backend 连接配置由各自的生命周期机制维护（cloud115 cookies 走保活回写与重新扫码）
- 删除是否允许取决于该媒体库是否仍被其他业务数据引用（Media / DownloadClient / ImportJob）
- cloud115 库的 cookies 与 qb 密码、Jackett key 同规格明文存库（`backend_config`），不引入额外加密框架；媒体库公开资源统一脱敏，任何创建、列表或更新响应均不返回 cookies
