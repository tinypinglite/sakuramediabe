# 可视化媒体导入

面向前端的“导入用户已有媒体”能力：浏览后端文件系统、选择导入源目录、提交后台导入任务，并对失败文件做删除/重命名/重导。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

## 资源与边界

- 导入源由用户在后端文件系统中浏览选择，导入目标为已配置的 `MediaLibrary`。
- 导入业务能力完全复用 `MediaImportService.import_from_source`，与下载任务导入共享同一套扫描、分组、元数据抓取与落库逻辑。
- 导入在后端**后台线程**（`DownloadImportRunner` 线程池）运行，客户端关闭不影响其运行；重连后通过任务中心查询进度。
- 失败文件清单复用 `ImportJob.failed_files`，不新增成功文件明细；模型层仅新增 `ImportJob.transfer_mode` 列（迁移 `20260607_01`）。
- `transfer_mode` 沿用现有 `auto`（默认，硬链接优先）与 `cleanup-source`（复制后删除源文件）两种语义，并持久化到作业以支撑失败重导沿用原模式。

## 接口

所有接口都需要登录鉴权（`Authorization: Bearer <token>`），并挂载在 `media-import` 标签下。

### 浏览目录

`GET /filesystem/entries?path=<绝对路径>`

- 仅允许浏览 `[media_import].browse_roots` 白名单根目录（含其子树），默认 `["/mnt"]`。
- `path` 缺省时：单根白名单直接列出该根内容；多根时返回各根概览（`path` 为空字符串）供前端选择下钻。
- 仅返回**一层**的**子目录**与**视频文件**两类条目（不递归）。
- 解析后落在白名单外的目标返回 `403 path_forbidden`；白名单外的子项（含指向白名单外的符号链接）与无权限项自动跳过。
- `parent` 仅在父目录仍位于白名单范围内时返回，越过白名单根不可再向上浏览。
- 路径不存在返回 `404 path_not_found`，非目录返回 `400 path_not_directory`，未配置任何根返回 `403 browse_not_configured`。

响应 `FilesystemListResponse`：

```json
{
  "path": "/data/incoming",
  "parent": "/data",
  "entries": [
    {"name": "movies", "path": "/data/incoming/movies", "type": "dir", "size": 0, "is_video": false},
    {"name": "ABP-123.mp4", "path": "/data/incoming/ABP-123.mp4", "type": "video", "size": 2147483648, "is_video": true}
  ]
}
```

### 触发目录导入

`POST /import-jobs`

```json
{"library_id": 1, "source_path": "/data/incoming/movies", "transfer_mode": "auto"}
```

- 校验媒体库存在；归一化 `source_path` 并校验落在白名单根目录内（不在范围内返回 `403 path_forbidden`）。
- `transfer_mode` 会持久化到 `ImportJob.transfer_mode`，供失败文件重导时沿用。
- **防重**：以 `media_import:{library_id}:{sha1(source_path)}` 作为 `BackgroundTaskRun.mutex_key`，同库同源路径正在导入时返回 `409 media_import_conflict`（响应附阻塞中的 `blocking_task_run_id`）。
- 接受后返回 `202`，并给出 `import_job_id` / `task_run_id` / `status`。
- 入队过程中任一步骤失败返回 `502 media_import_failed`，并回收 task_run（释放 mutex_key），不会遗留永久占用的互斥键。

### 查询导入作业

- `GET /import-jobs?page=&page_size=`：分页列表（按 id 倒序），含 `transfer_mode`。
- `GET /import-jobs/{id}`：详情，额外包含解析后的 `failed_files`（`path` / `reason` / `detail` / `kind`）。

`failed_files` 的 `kind` 用于区分条目性质，决定其是否可被删除/重命名/重导：

| kind | 含义 | 可删除/重命名/重导 |
|---|---|---|
| `file` | 单个媒体文件级失败（如 `movie_number_not_found`、`metadata_fetch_failed`、`media_import_failed`） | 是 |
| `skipped` | 主动跳过（如 `file_too_small`） | 否 |
| `warning` | 导入后告警（如 `source_delete_failed`、多字幕跳过） | 否 |
| `job` | 任务级失败（`import_job_crashed` / `bootstrap` / `interrupted`，`path` 为目录） | 否 |

> 旧数据若无 `kind` 字段，按 `reason` 自动回推分类。`imported/skipped/failed_count` 为历史操作统计，与 `failed_files` 条数不一一对应，前端核对时按 `kind` 区分展示。

进度与运行状态写入 `BackgroundTaskRun`，前端经 `GET /system/task-runs` 即可观测；任务中心也会广播 `task_run_created` / `task_run_updated` 事件。

### 重导失败文件

`POST /import-jobs/{id}/retry`

```json
{"files": ["/mnt/incoming/movies/ABP-123.mp4"]}
```

- 仅允许在作业处于终态（`completed` / `failed`）时操作，否则 `409 job_in_progress`。
- `files` 为空表示重导该作业**全部**可重导（`kind=file`）失败文件，否则只重导交集中的文件。
- **安全约束**：每个待重导路径都必须是该作业 `failed_files` 内 `kind=file` 的条目，否则 `403 file_not_in_failed_list`；并再次校验落在白名单根目录内。
- 以原作业的 `source_path` + `library` + **原 `transfer_mode`** 起一个**新的** `ImportJob` + `task_run`，并通过 `only_files` 把扫描范围限定到选中文件；retry 使用带 `retry` 前缀的独立 mutex_key，与全量导入互不冲突。
- 若选中文件均已不在源目录，作业判 `failed` 并记 `retry_sources_missing`，不会静默判 `completed`。

### 删除失败源文件

`DELETE /import-jobs/{id}/failed-files`，body `{"path": "..."}`

- 仅允许在作业终态时操作（否则 `409 job_in_progress`），且仅作用于 `kind=file` 条目（否则 `422 failed_file_not_actionable`），路径须落在白名单内。
- 目录路径一律拒绝（`422 cannot_delete_directory`），杜绝误删整目录。
- 删除文件（文件已不存在时视为成功），并从该作业 `failed_files` 移除该条记录，返回更新后的作业详情。

### 重命名失败源文件

`POST /import-jobs/{id}/failed-files/rename`，body `{"path": "...", "new_name": "ABP-123.mp4"}`

- 约束同删除；目标须为常规文件（目录/非常规文件返回 `422 cannot_rename_non_file`）。
- `new_name` 校验：非空、不为 `.`/`..`、不以 `.` 开头、不含路径分隔符与控制字符、长度 ≤255，否则 `422 invalid_new_name`。
- 目标已存在返回 `409 rename_target_exists`。
- 重命名后把 `failed_files` 中该条记录的 `path` 更新为新路径，保证后续仍可对新名重导且继续满足约束。

> 删除/重命名只改 `failed_files` 列表内容，不改 `imported/skipped/failed_count` 历史统计。

## 安全约束小结

- 目录浏览/导入/删除/重命名/重导一律限定在 `[media_import].browse_roots` 白名单根目录（含子树）内，解析后越界一律 `403`；白名单外的符号链接条目自动跳过。
- 删除/重命名/重导仅允许在作业终态、对 `kind=file` 的单文件失败项执行，且写操作前强制 `is_file` 校验，杜绝对目录的破坏性操作。
- 触发导入带 mutex 防重；入队失败会回收 task_run 释放 mutex_key。

## 业务态恢复

- `media_directory_import` 已注册启动恢复 handler（`MediaImportJobService.recover_orphaned_jobs`）：进程重启后把无存活 owner 进程、也无活跃后台线程的 `pending`/`running` 手动导入作业复位为 `failed` 并补记 `import_job_interrupted`，对应 `BackgroundTaskRun` 一并回收。
- 仍存活的 owner 进程（按 `task_run.owner_pid` 判定）或活跃后台线程的作业会被跳过，避免误杀正在运行的导入。
- 关联下载任务的导入由下载侧 `download_task_import` handler 负责，二者不重复处理。
