# Videos 域（非 JAV 视频）

## 定位

`videos` 域用于管理**无番号、无外部元数据**的非 JAV 视频（如个人收藏、国产/国外资源），与 JAV 的 `catalog`（`Movie`）体系完全平行。设计目标是「仅播放 + 整理」：

- 按**合集**（`VideoCollection`）组织，成员带 `position`，前端可按序顺序播放；
- 复用现有播放底座（缩略图、播放进度、**时刻** `MediaPoint`、流播放）。

不提供订阅、下载、推荐、相似度、以图搜图等 JAV 专属自动化能力。

## 数据模型

| 模型 | 表 | 说明 |
|---|---|---|
| `VideoItem` | `video_item` | 视频条目（标题/简介/封面/发布时间），1:N 关联 `Media`；封面 `cover_image` 由导入时读取视频**第 0 帧**生成 |
| `VideoCollection` | `video_collection` | 合集 |
| `VideoCollectionItem` | `video_collection_item` | 合集成员，`position` 决定顺序播放次序 |
| `VideoImportJob` | `video_import_job` | 异步视频导入作业：源路径、归属媒体库、导入模式、统计与失败文件，关联 `BackgroundTaskRun` 供进度可观测 |

### 播放底座解耦

`Media.movie` 由必填改为可空，并新增可空的 `Media.video_item`；一条 `Media` 归属 `movie`（JAV）或 `video_item`（非 JAV）之一。判定「是否 JAV」统一用 `media.movie_number`（外键原始值）。

- 播放底座（探测、扫描、缩略图生成、时刻增删查、流播放、删除级联）对两类 `Media` 同样生效；非 JAV 缩略图存放在 `videos/{video_item_id}/...` 命名空间下。
- discovery（以图搜图 / 时刻推荐 / 相似度）通过保留对 `Movie` 的 INNER JOIN 或显式 `movie` 非空过滤，**只覆盖 JAV**，不索引非 JAV 媒体。
- 跨域全局列表（全局时刻浏览、失效媒体列表、资源任务展示）改为 LEFT OUTER JOIN，番号相关字段可空，非 JAV 回退展示 `VideoItem.title`。

## 接口

鉴权与 DB 依赖与其它域一致（`db_deps` + `get_current_user`）。

### 视频条目 `/videos`

- `GET /videos`：分页列表，支持 `query`、`sort`（`created_at|title|duration|file_size` + `:asc|:desc`，默认 `created_at:desc`）。`duration`/`file_size` 取该条目第一条媒体（`Media.id` 最小）的时长/文件大小，无媒体按 0 参与排序。列表项额外返回 `duration_seconds`、`file_size_bytes`（同样取第一条媒体，无媒体为 0）。
- `POST /videos`：创建，body 含 `title`、`summary`、`release_date`。
- `GET /videos/{video_id}`：详情，含 `media_items`（复用影片媒体资源结构，含播放进度与时刻、签名播放地址）。
- `PATCH /videos/{video_id}`：局部更新（`title`、`summary`、`release_date`）。
- `DELETE /videos/{video_id}`：删除条目及其媒体（复用 `MediaService.delete_media` 清理文件/图片/向量）。

### 合集 `/video-collections`

- `GET /video-collections`、`POST`、`GET/PATCH/DELETE /{collection_id}`。
- `GET /{collection_id}/items`：返回成员，支持 `sort`（`position|created_at|title|duration|file_size` + `:asc|:desc`，默认 `position:asc`）。默认仍按手动 `position` 升序供顺序播放；`duration`/`file_size` 取成员第一条媒体的时长/文件大小，成员 `video` 也返回 `duration_seconds`、`file_size_bytes`。
- `POST /{collection_id}/items`（body `video_item_id`，追加到末尾）、`DELETE /{collection_id}/items/{item_id}`。
- `POST /{collection_id}/items/reorder`（body `ordered_item_ids`）：按给定顺序重写 `position`，要求恰好覆盖全部成员，否则 422。

### 导入 `/video-imports`

- `POST /video-imports`：**异步触发**，返回 `202` + `{video_import_job_id, task_run_id, status}`。body 含 `source_path`（目录或单文件）、**必填** `library_id`、`transfer_mode`（`auto`/`cleanup-source`，默认 `auto`）、可选 `collection_id`。
- `GET /video-imports/{video_import_job_id}`：查询作业状态、各计数与 `failed_files`。
- 进度实时查看：复用系统活动流 `GET /system/events/stream`（SSE，`task_run_updated` 事件）与 `GET /system/task-runs`，无需 videos 域另造推送。

导入语义（与 JAV 导入共用一套文件落库语义）：

- **文件搬运**：与 JAV 共用 `src/service/transfers/file_transfer.py` 的 `transfer_file`——`auto` 硬链接优先、失败回退复制；`cleanup-source` 复制后删除源文件（禁止作用于任一媒体库目录内，触发时即拒绝）。文件落入 `library_root/videos/<video_item_id>/<timestamp>/<filename>`。
- **媒体库归属**：`library_id` 必填，每条 `Media.library` 指向该库。
- **首帧封面**：导入每个视频时由 `VideoCoverService` 读取第 0 帧生成 `cover_image`；失败仅记日志、不阻断导入。
- **发布时间**：导入时由 `MediaMetadataProbeService` 从视频容器自身的 `creation_time` 元数据（容器优先、其次视频流）解析为 `release_date`；读不到或解析失败则留空，不用文件 mtime 兜底。
- **缩略图接手**：落库后 `ResourceTaskStateService.reset_for_requeue(...)` 置缩略图任务为待处理，由 `generate-media-thumbnails` 后台补齐。
- **去重**：先按 `Media.path` 命中跳过，再按内容指纹（`src/common/content_fingerprint.py`）跳过；探测复用 `MediaMetadataProbeService`。
- 后台执行复用 `DownloadImportRunner` 线程池 + `ActivityService.run_task`，触发防重依赖 `BackgroundTaskRun.mutex_key`；启动时 `recover_orphaned_jobs` 回收中断作业。
