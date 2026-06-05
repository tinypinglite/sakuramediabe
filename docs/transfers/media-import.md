# 可视化媒体导入

面向前端的“导入用户已有媒体”能力：浏览后端文件系统、选择导入源目录、提交后台导入任务，并对失败文件做删除/重命名/重导。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

## 资源与边界

- 导入源由用户在后端文件系统中浏览选择，导入目标为已配置的 `MediaLibrary`。
- 导入业务能力完全复用 `MediaImportService.import_from_source`，与下载任务导入共享同一套扫描、分组、元数据抓取与落库逻辑。
- 导入在后端**后台线程**（`DownloadImportRunner` 线程池）运行，客户端关闭不影响其运行；重连后通过任务中心查询进度。
- 失败文件清单复用 `ImportJob.failed_files`，不新增成功文件明细，不改动模型层。
- `transfer_mode` 沿用现有 `auto`（默认，硬链接优先）与 `cleanup-source`（复制后删除源文件）两种语义。

## 接口

所有接口都需要登录鉴权（`Authorization: Bearer <token>`），并挂载在 `media-import` 标签下。

### 浏览目录

`GET /filesystem/entries?path=<绝对路径>`

- `path` 缺省时从根目录 `/` 开始。
- 仅返回**一层**的**子目录**与**视频文件**两类条目（不递归）。
- 命中敏感目录黑名单（含子树）的目标返回 `403 path_forbidden`；黑名单子项与无权限项自动跳过。
- 路径不存在返回 `404 path_not_found`，非目录返回 `400 path_not_directory`。

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

- 校验媒体库存在；归一化 `source_path` 并做黑名单校验。
- **防重**：以 `media_import:{library_id}:{sha1(source_path)}` 作为 `BackgroundTaskRun.mutex_key`，同库同源路径正在导入时返回 `409 media_import_conflict`（响应附阻塞中的 `blocking_task_run_id`）。
- 接受后返回 `202`，并给出 `import_job_id` / `task_run_id` / `status`。

### 查询导入作业

- `GET /import-jobs?page=&page_size=`：分页列表（按 id 倒序）。
- `GET /import-jobs/{id}`：详情，额外包含解析后的 `failed_files`（`path` / `reason` / `detail`）。

进度与运行状态写入 `BackgroundTaskRun`，前端经 `GET /system/task-runs` 即可观测；任务中心也会广播 `task_run_created` / `task_run_updated` 事件。

### 重导失败文件

`POST /import-jobs/{id}/retry`

```json
{"files": ["/data/incoming/movies/ABP-123.mp4"]}
```

- `files` 为空表示重导该作业**全部**失败文件，否则只重导交集中的文件。
- **安全约束**：每个待重导路径都必须登记在该作业的 `failed_files` 内，否则返回 `403 file_not_in_failed_list`。
- 以原作业的 `source_path` + `library` 起一个**新的** `ImportJob` + `task_run`，并通过 `only_files` 把扫描范围限定到选中文件；retry 使用带 `retry` 前缀的独立 mutex_key，与全量导入互不冲突。

### 删除失败源文件

`DELETE /import-jobs/{id}/failed-files`，body `{"path": "..."}`

- 仅允许作用于该作业 `failed_files` 内登记过的路径，且不得命中黑名单。
- 删除文件（文件已不存在时视为成功），并从该作业 `failed_files` 移除该条记录，返回更新后的作业详情。

### 重命名失败源文件

`POST /import-jobs/{id}/failed-files/rename`，body `{"path": "...", "new_name": "ABP-123.mp4"}`

- 约束同上；`new_name` 不得包含路径分隔符。
- 目标已存在返回 `409 rename_target_exists`。
- 重命名后把 `failed_files` 中该条记录的 `path` 更新为新路径，保证后续仍可对新名重导且继续满足“仅限失败列表内”约束。

> 删除/重命名只改 `failed_files` 列表内容，不改 `imported/skipped/failed_count` 历史统计。

## 安全约束小结

- 目录浏览排除敏感目录黑名单（`[media_import].browse_blacklist`，含子树），不做白名单限制。
- 删除/重命名/重导这些写操作仅允许作用于某个 `ImportJob` 的 `failed_files` 中登记过的路径。
- 触发导入带 mutex 防重，避免同库同源路径并发重复导入。

## 已知限制

- 不接入业务态恢复：若**后端进程**重启，进行中的导入线程丢失，通用回收会把对应 `BackgroundTaskRun` 标记为 failed，但 `ImportJob.state` 可能残留 `running`（孤儿态，本期不清理）。客户端（前端）关闭不受影响。
