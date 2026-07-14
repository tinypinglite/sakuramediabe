# Downloads

## 资源说明

下载域负责对接 Jackett 与 qBittorrent，并管理本地可查询的下载状态。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

- Jackett 负责“搜索候选资源”
- qBittorrent 负责“实际下载”
- `DownloadTask` 是本地镜像数据，由“提交下载”或“同步任务”流程写入
- API 不提供 `DownloadTask` 的通用创建、更新、详情接口；只提供查询、实时状态与受控操作
- 删除下载任务会同时移除本地镜像；是否删除 qBittorrent 中的下载文件由调用方显式确认

## 边界说明

- 索引器配置继续使用系统级接口 `/indexer-settings`
- 下载客户端配置使用下载域接口 `/download-clients`
- 搜索结果 `DownloadCandidate` 为临时资源，不落库
- 提交下载使用命令式接口 `POST /download-requests`
- 定时任务可自动搜索“已订阅但缺失媒体且没有下载记录”的影片，并自动提交下载

## 设计目标

- 保持依赖方向为 `api -> service -> model`
- 让 Jackett 配置与 qBittorrent 客户端配置解耦
- 让搜索、提交下载、任务同步、媒体导入分成独立流程
- 允许一个系统级 Jackett 配置服务多个 `DownloadClient`
- 允许多个 `DownloadClient` 绑定不同媒体库
- 支持后续增加定时同步与自动导入，而不破坏 API 边界
- 支持后续增加自动搜索订阅影片资源，而不新增额外下载 API

## 内部定时任务

系统包含一个内部调度任务，用于自动搜索并提交“已订阅缺失影片”的下载请求。

行为约定：

- 仅处理 `is_subscribed = true` 的影片
- 仅处理不存在有效 `Media` 且不存在任何 `DownloadTask` 记录的影片
- 使用 Jackett 搜索 PT 与 BT 候选资源
- 默认采用 `4K > PT > 中字 > seeders > size_bytes` 的优先级选种
- 复用 `POST /download-requests` 对应的 service 提交下载，不新增 API

说明：

- 这里的下载候选 `4K` 标签仍然来自远端标题或索引器返回信息
- 它和本地 `media.special_tags` 的 `4K` 不是同一套规则；本地媒体侧的 `4K` 来自真实视频流解析

依赖前提：

- 已通过 `/indexer-settings` 配置可用的 `Indexer`
- 每个 `Indexer` 必须绑定一个 `DownloadClient`
- `DownloadClient` 必须绑定一个可用的 `MediaLibrary`

### 下载中种子小文件清理

系统包含一个内部调度任务（`download_small_file_cleanup`），默认每 5 分钟执行一次，用于清理下载中种子里夹带的小文件（sample / 垃圾文件），避免它们拖住整个下载任务、占用磁盘和带宽。

行为约定：

- 仅处理带 `sakuramedia` 系统标签（即经本系统添加）的种子；手动加入 qBittorrent 的种子不受影响
- 仅处理未完成（`progress < 1.0`）的种子
- 把种子内小于阈值的文件设为不下载（priority=0），并重命名为 `sakuramedia_need_delete_<uuid>` 标记
- 随后遍历下载客户端的 `local_root_path`，物理删除文件名含 `sakuramedia_need_delete` 的残留文件
- 已是 priority=0 的文件会跳过，保证反复执行的幂等性
- 没有下载中种子时跳过目录遍历：标记文件只可能由下载中种子产生，空扫只会无谓唤醒硬盘（下载盘常与媒体盘同盘）；若个别标记文件因删除失败遗留，会在下次出现下载中种子时补删

配置与运行：

- 小文件阈值由 `[downloads].small_file_cleanup_threshold_mb` 配置，默认 `256`（MB）
- 执行频率由 `[scheduler].download_small_file_cleanup_cron` 配置，默认 `*/5 * * * *`
- 可手动单次执行：`uv run python -m src.start.commands aps cleanup-download-small-files`

> ⚠️ 该任务不区分私有站（PT）与公开 BT 种子，凡带系统标签的下载中种子一律清理。若通过本系统下载 PT 站种子，清理小文件会破坏做种并影响分享率，请自行评估。

## 数据模型

### DownloadClient

`DownloadClient` 表示一个受系统管理的 qBittorrent 客户端配置。

为适配 Docker 或跨机器部署，下载路径拆为两类：

- `client_save_path`: qBittorrent 看到的保存路径
- `local_root_path`: 当前后端进程可访问的本地路径

如果后端和 qBittorrent 运行在同一文件系统上，这两个字段可以相同。

其中：

- 添加种子时，后端会在 `client_save_path` 下按番号拼出独立子目录（如 `/downloads/a/ABC-001`）作为 qBittorrent 的目标保存路径，使每个种子单独落盘，避免内容平铺到下载根目录后自动导入误扫整根
- 番号会做文件名净化（非法字符替换为下划线），杜绝路径穿越
- `client_save_path` 必须是 qBittorrent 进程实际可访问的路径
- `local_root_path` 仅用于后端同步任务和后续导入，不会传给 qBittorrent

```json
{
  "id": 1,
  "name": "client-a",
  "base_url": "http://localhost:8080",
  "username": "alice",
  "client_save_path": "/downloads/a",
  "local_root_path": "/mnt/qb/downloads/a",
  "media_library_id": 1,
  "has_password": true,
  "created_at": "2026-03-10T08:00:00",
  "updated_at": "2026-03-10T08:00:00"
}
```

### DownloadCandidate

`DownloadCandidate` 表示一次 Jackett 搜索返回的候选资源，不落库。

```json
{
  "source": "jackett",
  "indexer_name": "mteam",
  "indexer_kind": "pt",
  "resolved_client_id": 1,
  "resolved_client_name": "client-a",
  "movie_number": "ABC-001",
  "title": "ABC-001 4K 中文字幕",
  "size_bytes": 12884901888,
  "seeders": 18,
  "magnet_url": "",
  "torrent_url": "https://indexer.example/download/12345",
  "tags": ["4K", "中字"]
}
```

### DownloadTask

`DownloadTask` 表示本地数据库中保存的下载任务镜像。

```json
{
  "id": 100,
  "client_id": 1,
  "movie_number": "ABC-001",
  "name": "ABC-001 4K 中文字幕",
  "info_hash": "95a37f09c6d5aac200752f4c334dc9dff91e8cfc",
  "save_path": "/mnt/qb/downloads/a/ABC-001",
  "progress": 0.52,
  "download_state": "downloading",
  "import_status": "pending",
  "created_at": "2026-03-10T08:10:00",
  "updated_at": "2026-03-10T08:20:00"
}
```

说明：

- `save_path` 为后端可访问路径，应基于 `local_root_path` 计算
- `(client_id, info_hash)` 是任务幂等键
- `movie_number` 可以为空；同步阶段允许先按 `name` 解析，后续再补齐
- `import_status` 只反映本地导入流程，不直接映射 qBittorrent 状态

## 状态约定

### `download_state` 枚举

- `downloading`
- `completed`
- `paused`
- `failed`
- `stalled`
- `checking`
- `queued`

### `import_status` 枚举

- `pending`
- `running`
- `completed`
- `failed`
- `skipped`

## 实时进度与任务控制

客户端先通过 `GET /download-tasks` 分页加载全部历史记录，再以 `GET /download-tasks/stream` 订阅 qBittorrent 的实时状态。两个接口都支持 `client_id` 与 `movie_number` 筛选；SSE 首次为每个匹配下载客户端发送 `snapshot`，随后发送：

- `download_task_updated`：进度、速度、总大小、已下载量、ETA 与 qB 原始/归一化状态
- `download_task_removed`：qB 中任务被移除或本系统删除任务
- `download_client_status`：qB 客户端可用性，以及整体上下行速度
- `heartbeat`：连接保活

SSE 只服务在线实时展示，不把秒级进度写入数据库，也不改变现有 APS 同步和自动导入节奏。客户端断线后应重新查询任务列表并重新建立 SSE 连接。

暂停、恢复与删除均以本地 `task_id` 操作，后端会再次验证 qB 种子带有 `sakuramedia` 和对应 `client:<id>` 标签，手动加入 qB 的种子不会被操作。

- `POST /download-tasks/{task_id}/pause`
- `POST /download-tasks/{task_id}/resume`
- `DELETE /download-tasks/{task_id}?delete_files=false&confirm_delete_files=false`

`delete_files` 默认为 `false`。要连同 qB 下载文件删除，必须同时传 `delete_files=true` 和 `confirm_delete_files=true`；处于本地导入中的任务不能删除，避免与导入线程争用文件。删除成功后本地 `DownloadTask` 会被移除，已完成的媒体导入记录保持不变。

进度轮询周期由 `[downloads].progress_stream_poll_interval_seconds` 配置，默认 `1.0` 秒，允许范围为 `0.2` 至 `10` 秒。

## 端点总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/download-clients` | 获取下载客户端配置列表 |
| `POST` | `/download-clients` | 创建下载客户端配置 |
| `PATCH` | `/download-clients/{client_id}` | 更新下载客户端配置 |
| `DELETE` | `/download-clients/{client_id}` | 删除下载客户端配置 |
| `GET` | `/download-clients/{client_id}/test` | 测试 qBittorrent Web API 可用性 |
| `POST` | `/download-clients/{client_id}/storage-test` | 测试下载目录映射与硬链接能力 |
| `POST` | `/download-clients/probe/test` | 落库前预检 qBittorrent Web API 可用性 |
| `POST` | `/download-clients/probe/storage-test` | 落库前预检下载目录映射与硬链接能力 |
| `GET` | `/download-candidates` | 搜索番号的候选资源 |
| `POST` | `/download-requests` | 向指定客户端提交下载 |
| `GET` | `/download-tasks` | 分页查询全部下载历史 |
| `GET` | `/download-tasks/stream` | 订阅 qBittorrent 实时进度 SSE |
| `POST` | `/download-tasks/{task_id}/pause` | 暂停受管下载任务 |
| `POST` | `/download-tasks/{task_id}/resume` | 恢复受管下载任务 |
| `DELETE` | `/download-tasks/{task_id}` | 移除受管种子与本地任务镜像 |

## 详细接口定义

### Endpoint

`GET /download-clients`

### Purpose

返回下载客户端配置列表，按 `created_at desc, id desc` 排序。

### Auth

需要 Bearer Token。

### Success Responses

- `200 OK`: 返回下载客户端配置数组

### Example Response

```json
[
  {
    "id": 1,
    "name": "client-a",
    "base_url": "http://localhost:8080",
    "username": "alice",
    "client_save_path": "/downloads/a",
    "local_root_path": "/mnt/qb/downloads/a",
    "media_library_id": 1,
    "has_password": true,
    "created_at": "2026-03-10T08:00:00",
    "updated_at": "2026-03-10T08:00:00"
  }
]
```

### Endpoint

`POST /download-clients`

### Purpose

创建一个下载客户端配置。

### Auth

需要 Bearer Token。

### Request Body

```json
{
  "name": "client-a",
  "base_url": "http://localhost:8080",
  "username": "alice",
  "password": "secret",
  "client_save_path": "/downloads/a",
  "local_root_path": "/mnt/qb/downloads/a",
  "media_library_id": 1
}
```

### Validation

- `name` 必须唯一
- `base_url` 必须是 `http` 或 `https`
- `client_save_path` 必须是绝对路径
- `local_root_path` 必须是绝对路径
- `media_library_id` 必须存在

### Success Responses

- `201 Created`: 返回创建后的配置

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: `media_library_id` 不存在
- `409 Conflict`: `name` 已存在
- `422 Unprocessable Entity`: 字段校验失败

### Endpoint

`PATCH /download-clients/{client_id}`

### Purpose

更新下载客户端配置，允许部分字段更新。

### Rules

- 未传 `password` 时保持原密码
- 请求体为空时返回 `422`

### Auth

需要 Bearer Token。

### Path Params

- `client_id`: 下载客户端 ID

### Request Body

```json
{
  "name": "client-main",
  "base_url": "https://qb.example.com",
  "username": "bob",
  "password": "new-secret",
  "client_save_path": "/downloads/main",
  "local_root_path": "/data/downloads/main",
  "media_library_id": 2
}
```

### Success Responses

- `200 OK`: 返回更新后的配置

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: `client_id` 或 `media_library_id` 不存在
- `409 Conflict`: `name` 冲突
- `422 Unprocessable Entity`: 请求为空或字段校验失败

### Endpoint

`DELETE /download-clients/{client_id}`

### Purpose

删除下载客户端配置。

### Rules

- 若仍有关联 `DownloadTask`，返回 `409`
- 删除配置不直接删除 qBittorrent 中已有任务

### Auth

需要 Bearer Token。

### Path Params

- `client_id`: 下载客户端 ID

### Success Responses

- `204 No Content`: 删除成功

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 下载客户端不存在
- `409 Conflict`: 仍有关联下载任务，无法删除

### Endpoint

`GET /download-clients/{client_id}/test`

### Purpose

实时测试下载客户端对应的 qBittorrent Web API 是否可用。

该接口只执行只读检测：登录 qBittorrent，并读取 qBittorrent 应用版本与 Web API 版本。它不会读取种子列表、不会添加下载任务、不会修改远端标签，也不会检查 `client_save_path` / `local_root_path` 路径映射或硬链接能力。

### Auth

需要 Bearer Token。

### Path Params

- `client_id`: 下载客户端 ID

### Success Responses

- `200 OK`: 始终返回本次检测结果；qBittorrent 不可用时通过 `healthy=false` 与 `error` 字段表达

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 下载客户端不存在

### Example Response

```json
{
  "healthy": true,
  "checked_at": "2026-07-03T12:00:00",
  "client_id": 1,
  "client_name": "client-a",
  "base_url": "http://localhost:8080",
  "elapsed_ms": 18,
  "version": "5.0.4",
  "web_api_version": "2.11.4",
  "error": null
}
```

失败示例：

```json
{
  "healthy": false,
  "checked_at": "2026-07-03T12:00:00",
  "client_id": 1,
  "client_name": "client-a",
  "base_url": "http://localhost:8080",
  "elapsed_ms": 1002,
  "version": null,
  "web_api_version": null,
  "error": {
    "type": "qbittorrent_request_error",
    "message": "login failed"
  }
}
```

### Endpoint

`POST /download-clients/{client_id}/storage-test`

### Purpose

主动测试下载客户端的目录映射与硬链接能力。

该接口会在后端可见的 `local_root_path/.sakuramedia-diagnostics/<uuid>/` 下创建哨兵文件，再通过 qBittorrent 的目录读取接口检查 qB 视角下的 `client_save_path/.sakuramedia-diagnostics/<uuid>/` 是否能看到同名文件。若 qB 能看到哨兵文件，则认为 `local_root_path` 与 `client_save_path` 映射到同一目录。

目录映射通过后，接口会尝试从哨兵文件硬链接到绑定媒体库 `root_path/.sakuramedia-diagnostics/<uuid>/sentinel.link`，用于判断后续导入是否能使用硬链接。硬链接失败不会使整体检测失败，因为导入流程会回退为复制，但响应会返回 warning。

该接口不检测 qBittorrent 默认保存路径。无论检测成功或失败，后端都会尽力清理本次创建的哨兵文件、硬链接目标和空诊断目录。

### Auth

需要 Bearer Token。

### Path Params

- `client_id`: 下载客户端 ID

### Success Responses

- `200 OK`: 始终返回本次检测结果；目录映射失败时 `healthy=false`，硬链接失败时 `healthy=true` 且包含 `warnings`

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 下载客户端不存在

### Example Response

```json
{
  "healthy": true,
  "checked_at": "2026-07-03T12:05:00",
  "client_id": 1,
  "client_name": "client-a",
  "elapsed_ms": 24,
  "warnings": [],
  "directory_mapping": {
    "status": "ok",
    "client_save_path": "/downloads/a",
    "local_root_path": "/mnt/qb/downloads/a",
    "probe_remote_dir": "/downloads/a/.sakuramedia-diagnostics/4f9b",
    "probe_local_dir": "/mnt/qb/downloads/a/.sakuramedia-diagnostics/4f9b",
    "sentinel_visible_to_qb": true,
    "error": null
  },
  "hardlink": {
    "status": "ok",
    "supported": true,
    "source_path": "/mnt/qb/downloads/a/.sakuramedia-diagnostics/4f9b/sentinel.txt",
    "target_path": "/media/library/main/.sakuramedia-diagnostics/4f9b/sentinel.link",
    "error": null
  }
}
```

硬链接失败示例：

```json
{
  "healthy": true,
  "checked_at": "2026-07-03T12:05:00",
  "client_id": 1,
  "client_name": "client-a",
  "elapsed_ms": 31,
  "warnings": ["下载目录到媒体库不支持硬链接，导入会回退为复制"],
  "directory_mapping": {
    "status": "ok",
    "client_save_path": "/downloads/a",
    "local_root_path": "/mnt/qb/downloads/a",
    "probe_remote_dir": "/downloads/a/.sakuramedia-diagnostics/4f9b",
    "probe_local_dir": "/mnt/qb/downloads/a/.sakuramedia-diagnostics/4f9b",
    "sentinel_visible_to_qb": true,
    "error": null
  },
  "hardlink": {
    "status": "failed",
    "supported": false,
    "source_path": "/mnt/qb/downloads/a/.sakuramedia-diagnostics/4f9b/sentinel.txt",
    "target_path": "/media/library/main/.sakuramedia-diagnostics/4f9b/sentinel.link",
    "error": {
      "type": "hardlink_not_supported",
      "message": "Invalid cross-device link"
    }
  }
}
```

### Endpoint

`POST /download-clients/probe/test`

### Purpose

在下载客户端尚未落库的情况下，直接使用表单提供的连接信息预检 qBittorrent Web API 可用性。用于「新建」或「编辑」表单里的即时测试按钮，探测过程不写数据库。

行为与 `GET /download-clients/{client_id}/test` 完全一致：只登录 qBittorrent 并读取应用版本与 Web API 版本，不读取种子列表、不修改远端标签。

密码合并规则（对齐「编辑时密码留空 = 不改」约定）：

- `password` 非空：直接用 payload 的密码进行探测。
- `password` 为空/缺省：必须同时提供 `client_id`，后端会读取该客户端已保存的密码。
- `password` 为空且未提供 `client_id`：返回 `422 invalid_download_client_password`。

### Auth

需要 Bearer Token。

### Request Body

```json
{
  "base_url": "http://localhost:8080",
  "username": "alice",
  "password": "s3cret",
  "client_id": null
}
```

字段说明：

- `base_url`: 必填，qBittorrent Web UI 地址，必须是 `http` 或 `https`
- `username`: 必填
- `password`: 可空/缺省；空时必须提供 `client_id`
- `client_id`: 可空；仅用于「密码留空时合并 DB 原密码」，不会因此写库

### Success Responses

- `200 OK`: 始终返回本次检测结果；qBittorrent 不可用时通过 `healthy=false` 与 `error` 字段表达；未落库场景下 `client_id=0`、`client_name=""`

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 传入了不存在的 `client_id`
- `422 Unprocessable Entity`: 参数校验失败（`base_url`、`username`、`password` 组合非法）

### Example Response

```json
{
  "healthy": true,
  "checked_at": "2026-07-03T12:00:00",
  "client_id": 0,
  "client_name": "",
  "base_url": "http://localhost:8080",
  "elapsed_ms": 18,
  "version": "5.0.4",
  "web_api_version": "2.11.4",
  "error": null
}
```

### Endpoint

`POST /download-clients/probe/storage-test`

### Purpose

在下载客户端尚未落库的情况下，直接使用表单提供的连接信息与路径配置预检下载目录映射与硬链接能力。用于「新建」或「编辑」表单里的即时测试按钮，探测过程不写数据库。

行为与 `POST /download-clients/{client_id}/storage-test` 完全一致：在 `local_root_path/.sakuramedia-diagnostics/<uuid>/` 写哨兵文件，通过 qBittorrent 的目录读取接口检查 `client_save_path/.sakuramedia-diagnostics/<uuid>/` 是否可见；随后尝试硬链接到绑定媒体库 `root_path/.sakuramedia-diagnostics/<uuid>/sentinel.link`。硬链接失败会以 `warnings` 返回，不影响 `healthy`。无论成功失败都会尽力清理临时文件与空诊断目录。

`media_library_id` 必填，决定硬链接目标根路径。`password` 处理规则与 `POST /download-clients/probe/test` 相同。

### Auth

需要 Bearer Token。

### Request Body

```json
{
  "base_url": "http://localhost:8080",
  "username": "alice",
  "password": "s3cret",
  "client_save_path": "/downloads/a",
  "local_root_path": "/mnt/qb/downloads/a",
  "media_library_id": 1,
  "client_id": null
}
```

字段说明：

- `base_url` / `username` / `password` / `client_id`: 同 `POST /download-clients/probe/test`
- `client_save_path`: 必填，qBittorrent 视角下的下载根路径，必须是绝对路径
- `local_root_path`: 必填，当前后端进程可访问的下载根路径，必须是绝对路径
- `media_library_id`: 必填，绑定媒体库 ID，用于确定硬链接目标根路径

### Success Responses

- `200 OK`: 始终返回本次检测结果；目录映射失败时 `healthy=false`，硬链接失败时 `healthy=true` 且包含 `warnings`；未落库场景下 `client_id=0`、`client_name=""`

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 传入了不存在的 `client_id`
- `422 Unprocessable Entity`: 参数校验失败（`base_url` / `username` / 路径 / 密码组合非法，或 `media_library_id` 不存在）

### Example Response

```json
{
  "healthy": true,
  "checked_at": "2026-07-03T12:05:00",
  "client_id": 0,
  "client_name": "",
  "elapsed_ms": 24,
  "warnings": [],
  "directory_mapping": {
    "status": "ok",
    "client_save_path": "/downloads/a",
    "local_root_path": "/mnt/qb/downloads/a",
    "probe_remote_dir": "/downloads/a/.sakuramedia-diagnostics/4f9b",
    "probe_local_dir": "/mnt/qb/downloads/a/.sakuramedia-diagnostics/4f9b",
    "sentinel_visible_to_qb": true,
    "error": null
  },
  "hardlink": {
    "status": "ok",
    "supported": true,
    "source_path": "/mnt/qb/downloads/a/.sakuramedia-diagnostics/4f9b/sentinel.txt",
    "target_path": "/media/library/main/.sakuramedia-diagnostics/4f9b/sentinel.link",
    "error": null
  }
}
```

### Endpoint

`GET /download-candidates`

### Purpose

根据番号搜索 Jackett 候选资源。

### Auth

需要 Bearer Token。

### Query Params

- `movie_number`: 必填，番号，大小写不敏感
- `indexer_kind`: 可选，`pt` 或 `bt`

### Behavior

- 服务读取 `/indexer-settings` 对应的当前运行时配置
- 当 `movie_number` 以 `FC2` 开头（含 `FC2-PPV-xxxx`）时，调用 Jackett 会仅使用数字部分作为查询词
- 结果为临时数据，不写入数据库
- 按“更高做种数优先，其次更大体积优先”排序返回

### Success Responses

- `200 OK`: 返回候选资源数组

### Example Response

```json
[
  {
    "source": "jackett",
    "indexer_name": "mteam",
    "indexer_kind": "pt",
    "resolved_client_id": 1,
    "resolved_client_name": "client-a",
    "movie_number": "ABC-001",
    "title": "ABC-001 4K 中文字幕",
    "size_bytes": 12884901888,
    "seeders": 18,
    "magnet_url": "",
    "torrent_url": "https://indexer.example/download/12345",
    "tags": ["4K", "中字"]
  }
]
```

### Error Responses

- `401 Unauthorized`: 未认证
- `422 Unprocessable Entity`: 查询参数非法
- `502 Bad Gateway`: Jackett 请求失败

### Endpoint

`POST /download-requests`

### Purpose

提交一个候选资源；若未显式指定 `client_id`，服务端会按 `candidate.indexer_name` 自动解析目标下载器。

### Auth

需要 Bearer Token。

### Request Body

```json
{
  "movie_number": "ABC-001",
  "candidate": {
    "source": "jackett",
    "indexer_name": "mteam",
    "indexer_kind": "pt",
    "title": "ABC-001 4K 中文字幕",
    "size_bytes": 12884901888,
    "seeders": 18,
    "magnet_url": "",
    "torrent_url": "https://indexer.example/download/12345",
    "tags": ["4K", "中字"]
  }
}
```

### Behavior

- 若请求体包含 `client_id`，优先使用显式指定的目标 `DownloadClient`
- 若未传 `client_id`，根据 `candidate.indexer_name` 查找数据库中的 `Indexer`，并使用其绑定的 `DownloadClient`
- 按候选资源优先使用 `magnet_url`，否则使用 `torrent_url`
- 添加种子时，在 `DownloadClient.client_save_path` 下按番号拼出独立子目录传给 qBittorrent 作为保存路径（如 `/downloads/a/ABC-001`），避免内容平铺到下载根目录
- 提交成功后，立即按 `(client_id, info_hash)` 幂等写入或更新本地 `DownloadTask`
- qBittorrent 中的任务应统一打上系统标签，便于后续同步
- 若远端已存在相同任务，可返回现有本地任务而不是报错

### Path Semantics

- `client_save_path` 是写给 qBittorrent 的路径，例如 `/downloads/a`
- `local_root_path` 是后端访问同一份文件时使用的路径，例如 `/mnt/qb/downloads/a`
- 若 qBittorrent 返回的任务路径基于 `client_save_path`，同步阶段应将其映射为 `local_root_path` 下的本地可访问路径，再写入 `DownloadTask.save_path`

### Success Responses

- `201 Created`: 成功创建本地任务镜像
- `200 OK`: 远端任务已存在，返回现有本地任务

### Example Response

```json
{
  "task": {
    "id": 100,
    "client_id": 1,
    "movie_number": "ABC-001",
    "name": "ABC-001 4K 中文字幕",
    "info_hash": "95a37f09c6d5aac200752f4c334dc9dff91e8cfc",
    "save_path": "/mnt/qb/downloads/a/ABC-001",
    "progress": 0.0,
    "download_state": "queued",
    "import_status": "pending",
    "created_at": "2026-03-10T08:10:00",
    "updated_at": "2026-03-10T08:10:00"
  },
  "created": true
}
```

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 显式传入的 `client_id` 不存在
- `422 Unprocessable Entity`: 请求体非法，候选资源既无 `magnet_url` 也无 `torrent_url`，或 `candidate.indexer_name` 未配置
- `502 Bad Gateway`: qBittorrent 或下载源请求失败

## 同步与导入策略

- `POST /download-requests` 负责“提交远端任务 + 写入首次本地镜像”
- 定时任务可复用同一个同步服务，不新增独立 API 语义
- 自动导入属于调度策略，不额外要求新增公开 API

## 与当前实现的主要差异

- `DownloadClient.download_root_path` 调整为 `client_save_path` 与 `local_root_path`
- 新增临时资源 `DownloadCandidate`
- 新增命令式接口 `/download-requests`
- `DownloadTask` 仍保持只读镜像定位
