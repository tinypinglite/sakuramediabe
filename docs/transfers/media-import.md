# 统一媒体导入

JAV 与普通视频、本地文件系统与 115 网盘共用一个异步入口。导入执行记录只使用
`BackgroundTaskRun`，不再维护平行的 `ImportJob` / `VideoImportJob` 台账。

## 接口

所有接口均需要登录鉴权，并挂载 `db_deps`。

### 浏览本地来源

`GET /filesystem/entries?path=<绝对路径>`

- 仅允许访问 `[media_import].browse_roots` 白名单内的目录。
- 只返回一层子目录和视频文件；符号链接解析后越界的条目会被过滤。
- `path` 缺省时返回可用根目录或唯一根目录的内容。

### 创建导入任务

`POST /imports`，成功返回 `202`：

```json
{
  "task_run_id": 123,
  "task_key": "library_import",
  "state": "pending"
}
```

请求统一使用以下字段：

```json
{
  "media_kind": "jav",
  "backend": "local",
  "library_id": 1,
  "source_path": "/data/incoming/ABP-123",
  "transfer_mode": "auto"
}
```

- `media_kind`：`jav | video`。
- `backend`：`local | cloud115`，必须与目标媒体库后端一致。
- `source_path` / `source_cid` / `source_fid` 必须恰好提供一个；JAV 不支持单 FID。
- `collection_id` 仅供普通视频使用。
- 本地支持 `auto | cleanup-source`；115 固定为 `cleanup-source`，即远端 move。

四种组合示例：

```json
{"media_kind":"jav","backend":"local","library_id":1,"source_path":"/data/JAV"}
{"media_kind":"jav","backend":"cloud115","library_id":2,"source_cid":"10001"}
{"media_kind":"video","backend":"local","library_id":1,"source_path":"/data/video.mp4","collection_id":3}
{"media_kind":"video","backend":"cloud115","library_id":2,"source_fid":"20001","collection_id":3}
```

## TaskRun 与互斥

任务行、完整参数和关联下载记录在同一事务中提交；提交前 worker 不可见半成品，事务失败也不会留下
占用 mutex 的孤儿任务。

为保证 move / `cleanup-source` 不会并发处理互相重叠的目录树，本地导入共用一把全局 mutex，
所有本地库和来源串行；所有 115 媒体库共用一把写入 mutex，CID/FID、媒体种类、下载自动入库和媒体秒传
全部串行。不同后端之间仍可并行，
本地与 115 也可并行；import lane 仍能同时服务这些独立互斥域及其它任务。运行状态和结果通过
TaskRun 列表或活动聚合接口轮询：

- `GET /system/task-runs?task_key=library_import`：按返回的 `id` 匹配创建接口给出的 `task_run_id`；
- `GET /system/activity/bootstrap?task_key=library_import`：一次取得任务列表和通知，通知可通过
  `related_task_run_id` 与本次导入关联。

稳定结果契约只包含 `imported_count`、`skipped_count`、`failed_count`。部分执行组合可能附带
`new_playable_movies` 等可选摘要字段，调用方不应假定所有后端都返回它们。单文件失败会记录
结构化日志并继续处理；扫描或执行器顶层异常才令 TaskRun 进入 failed。

本次不再提供 `/import-jobs`、`/video-imports` 的列表、详情、失败文件 retry、rename 或 delete。
重试方式是以同一请求重新调用 `POST /imports`，执行器按整源幂等规则收敛。

## 数据与幂等语义

### 本地

- `auto` 优先硬链接，失败时复制；`cleanup-source` 复制成功后删除源。
- `cleanup-source` 禁止作用于任一媒体库目录内部。
- JAV 按路径与内容指纹去重；普通视频同样按路径、内容指纹去重。
- 重跑始终重新扫描整个来源，不持久化单文件失败列表。

### 115

- 新旧运行时一律 move，不再执行 copy；历史 copy 作业不会恢复执行。
- move 保持 fid / pickcode 不变。Media 在搬运前登记；若进程在登记后、搬运前中断，整源重跑
  会以 `locator.fid` 精确识别同一个源文件并补完移动。
- 同 SHA1 但 fid 不同视为独立重复源，只跳过并保留源文件，不会误删。
- JAV 字幕、视频技术元数据探测等依赖源文件的步骤在 move 前完成；单文件失败不影响其它文件。
- 115 普通视频只保留 move 分支，目标布局为 `videos/<video_item_id>/<version>/`。

## 下载自动入库

qBittorrent 与 115 离线下载都通过 `ImportTaskService` 创建 `library_import` TaskRun。
`DownloadTask.import_task_run_id` 精确关联本轮执行，`ON DELETE SET NULL`；`import_status` 继续作为
列表筛选字段：

- TaskRun completed 且 `failed_count == 0`：`completed`；
- TaskRun failed 或摘要 `failed_count > 0`：`failed`；
- 启动恢复发现精确关联的 TaskRun 已失败：重置为 `pending`，等待整源重试。

新片提醒在统一边界按 TaskRun 使用 `create_once`，重复恢复不会重复发通知。

## Breaking change

从 v0.4.21 升级时，迁移 `20260821_01_consolidate_task_runtime` 新增下载任务的 TaskRun 外键和索引，
把旧 `running` 下载导入重置为 `pending`；同时清空通知、删除 `import_job`、`video_import_job`、
`subtitle_import_job`、旧系统事件与通用资源任务台账，并终止旧的活动 TaskRun。旧表里的未完成作业直接放弃，
不做兼容续跑；已有媒体数据和订阅搜索状态会迁入当前领域字段。
