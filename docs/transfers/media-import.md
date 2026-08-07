# 可视化媒体导入

面向前端的“导入用户已有媒体”能力：浏览导入源（本地文件系统 / 115 网盘目录）、提交后台导入任务，并对失败文件做删除/重命名/重导。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

## 资源与边界

- 导入源由用户浏览选择：本地库在后端文件系统中选目录；cloud115 库在 115 网盘目录树中选目录（浏览端点见 `docs/playback/media-libraries.md`）。导入目标为已配置的 `MediaLibrary`。
- 本地导入复用 `MediaImportService.import_from_source`；cloud115 导入走 `Cloud115ImportService`（云端复制，仅技术元数据探测按预算 Range 读取，不完整下载落地），两者共享 `ImportJob` 模型、任务中心进度与失败清单结构。
- 导入在后端**后台线程**（`DownloadImportRunner` 线程池）运行，客户端关闭不影响其运行；重连后通过任务中心查询进度。
- 失败文件清单复用 `ImportJob.failed_files`；模型层含 `ImportJob.transfer_mode`（迁移 `20260607_01`）与 `ImportJob.source_cid`（迁移 `20260714_02`，cloud115 作业专用，本地作业为 NULL——前端可据此区分作业类型）。
- `transfer_mode` 按库类型取值：本地 `auto`（默认，硬链接优先）/ `cleanup-source`（复制后删源）；cloud115 `cleanup-source`（默认，云端直接移动进库）/ `copy`（云端复制，保留源文件）。旧 `move` 仅作为兼容输入，创建作业时统一保存为 `cleanup-source`。

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

按请求形状分派：`source_path` 与 `source_cid` **恰好其一**（同时给或都不给返回 `422`）。

**本地目录导入**：

```json
{"library_id": 1, "source_path": "/data/incoming/movies", "transfer_mode": "auto"}
```

- 校验媒体库存在；归一化 `source_path` 并校验落在白名单根目录内（不在范围内返回 `403 path_forbidden`）。
- `transfer_mode` 缺省为 `auto`，会持久化到 `ImportJob.transfer_mode`，供失败文件重导时沿用。
- **防重**：以 `media_import:{library_id}:{sha1(source_path)}` 作为 `BackgroundTaskRun.mutex_key`，同库同源路径正在导入时返回 `409 media_import_conflict`（响应附阻塞中的 `blocking_task_run_id`）。
- 接受后返回 `202`，并给出 `import_job_id` / `task_run_id` / `status`。
- 入队过程中任一步骤失败返回 `502 media_import_failed`，并回收 task_run（释放 mutex_key），不会遗留永久占用的互斥键。

**115 网盘导入**（目标库须为 `backend=cloud115`，否则 `422 media_library_backend_mismatch`）：

```json
{"library_id": 2, "source_cid": "3428707991046116541", "transfer_mode": "cleanup-source"}
```

- `transfer_mode` 只接受 `cleanup-source`（默认）/ `copy`；旧 `move` 兼容为 `cleanup-source`，其它值返回 `422 invalid_transfer_mode`。
- **服务端防御校验**（前端目录选择器已按浏览端点返回的 `root_cid` 禁选，这里兜底）：源目录不能是库管理目录、不能在其内部（`422 cloud115_source_inside_library`）、也不能包含它——含选中网盘根目录的情形（`422 cloud115_source_contains_library`）。
- 115 导入不设置库级 `mutex_key`；每个作业各自创建 Activity 并进入后台线程池，同一媒体库允许多个导入或秒传作业并行入队。
- **源目录扫描的请求构成**（`collect_cloud115_source_files`）：一次 `iter_files_recursive` 由服务端展开整棵子树只返文件（`ceil(文件数/1150)` 次，与目录层数无关），随后**只为视频文件**解析父目录名——先对源目录做**一层** `list_dir` 覆盖直属子目录，仅剩的深层目录才逐个 `dir_info`（其面包屑一次即可还原完整相对链）。因此请求数与**源目录树的目录总数解耦**：不含视频的目录（例如已导入完成、只剩空壳的历史离线任务目录）一次都不会被访问。
- 导入管线前半段两模式共用：递归枚举源目录 → 按「父目录名/文件名」识别番号 → 按 115 全量 sha1 对账。之后按模式分岔：
  - `copy`：云端复制到 `sakuramedia/jav/` → re-list 目标目录拿新 fid/pickcode → 逐文件改名并按 fid 查询确认 → 探测技术元数据 → 事务登记 Media → 下载并登记字幕。源文件始终保留；有效文件探测失败时整组不入库，重试按 SHA1 复用已经复制或改名的目标文件。
  - `cleanup-source`：用**源 pickcode** 探测技术元数据 → 下载并登记字幕 → 建版本目录 → 事务登记 Media（locator 直接用源 fid/pickcode）→ `files/move` 移动进库 → 逐文件改名并按 fid 确认 → 删除远端源字幕。依赖源文件的步骤全部前置，探测或字幕失败时源原地未动、整组可完整重导。
- 技术元数据探测按累计 Range 响应设置 `64 MiB` 读取预算，与影片总大小无关；CDN 若忽略 Range 返回超预算整文件，会在读取响应体前拒绝。
- 多分部（VR/FC2）**不做合并**：每个文件一条 Media 挂同一影片；与本地 JAV 导入语义一致，均不做 ffmpeg 拼接。
- 配对的 `.srt` 字幕**不复制到 115**：下载到本地 `movies/{shard}/{番号}/subtitles/` 并登记 `Subtitle`。`cleanup-source` 把字幕下载排在搬运之前，失败即整组终止、源完好可重导；搬运完成后才删掉远端源 srt（视频本身已经移走，不需要删）。`copy` 下字幕失败仅告警。
- **字幕配对规则**（本地与 115 统一）：在视频同目录内，从**字幕文件名解析番号**，解析出且与影片番号一致才算配对（纯番号匹配，不再要求与视频同名）。因此 `ABP-123.chs.srt` 这类带语种/修饰后缀的字幕也能配上；而文件名里解析不出番号的字幕（如 `01.srt`、随意命名的 `sub.srt`）不会被配对。判定收口在 `src/common/movie_numbers.py` 的 `subtitle_matches_movie_number()`。
- **手动字幕导入**：用户还可以在 GUI 里选择一个字幕目录，由后端递归扫描 `.srt` 并导入到对应影片（命名规则与上述配对一致，见 `docs/catalog/movies.md` 的「手动字幕导入」）。
- 115 标记违规的文件（`ic=1`）按 `valid=false` 登记并记 `cloud115_file_censored` 告警（拿不到直链也播不了）。
- **幂等**：
  - `copy` 中断重跑以「目标目录 sha1 对账」收敛——已搬的跳过搬运、没改名的补改名、没登记的补登记；复制产生新 fid/pickcode，登记一律以复制后 re-list 目标目录的条目为准。
  - `cleanup-source` 不读目标目录：移动保持 fid/pickcode 不变，已搬走的文件下一轮扫不到，"已登记但没搬走"的源会被扫到并按 `locator.fid == 源 fid` 认出来，补完搬运即收敛。库内已有同 sha1 且 `locator.fid` 不同才判定为多余副本，只删源、不再搬运。
- **自动离线导入完成后删除来源任务目录**（连同 nfo / 封面 / 种子 / 判定过小的样本等非视频残留，进 115 回收站）。三重前提缺一不可：来源是软件自建的下载缓冲区（`managed_download_source`，用户手动选的目录一律不动）、本次**零失败项**（番号识别不出的视频计入失败，因此不会误删还需重导的内容）、来源不是缓冲区根目录本身。删除失败只记 `source_delete_failed` 告警，不把作业翻成失败——文件已入库。
- **批量节奏控制**：导入作业的 SDK client 以 `batch_pacing=True` 创建，每累计 `cloud115_batch_rest_every_requests`（默认 30）个 **webapi 域**请求就额外随机长休 10~30 秒；取直链（proapi）、离线（lixian）、CDN Range 读不计数。休息期间通过进度事件 `pace_waiting` 显式透出，避免与卡死混淆。播放取直链、GUI 浏览目录等交互路径不启用。
- cookies 失效返回 `422 cloud115_cookies_invalid`（引导重新扫码）；115 限流返回 `429 cloud115_rate_limited`；触发 WAF 风控（裸 HTTP 405，或 webapi 域的裸 HTTP 400——115 应用层错误一律是 200 + `state=false` + errno，故该域裸 4xx 只可能是 WAF）返回 `429 cloud115_risk_control`；目录重名返回 `409 cloud115_duplicate_name`；其它上游错误 `502 cloud115_upstream_error`。
- **目录创建是竞态安全的**：`find_or_create_subdir` 在「扫描未命中 → mkdir」的窗口里若被并发作业抢先，115 回 `errno=20004` 拒绝重名，此时重扫一遍取对方建好的 cid 收敛，两边拿到同一个目录。番号实体目录撞到这种情况时，`created` 标志会**回落为 False** —— 目录不是本轮新建的、可能已有文件，必须继续跑 SHA1 对账，否则会重复导入。这条自愈路径在 `d70a532` 移除库级 mutex 后是必需的（`mutex_key=None`，且 API 手动触发与 APS 定时任务本来也无法靠单个 mutex 串起来）。

### 查询导入作业

- `GET /import-jobs?page=&page_size=`：分页列表（按 id 倒序），含 `transfer_mode`、作业状态 `state` 及其中文说明 `state_label`。
- `GET /import-jobs/{id}`：详情，额外包含解析后的 `failed_files`（`path` / `reason` / `detail` / `kind`，并附中文说明 `reason_label` / `kind_label`）。

> `state_label` / `reason_label` / `kind_label` 由后端依据[状态枚举与中文说明](#状态枚举与中文说明)集中映射注入，前端可直接展示，无需自行维护文案。

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
- **cloud115 作业**：`files` 传的是失败清单里的**源内相对路径**（如 `ABP-123/movie.mp4`），重导按原 `source_cid` 重新枚举后按相对路径取子集；语义与本地一致。

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

> **cloud115 作业不支持删除/重命名失败源文件**（那是对用户 115 目录的写操作），两端点对 cloud115 作业一律返回 `422 cloud115_failed_file_not_actionable`；请在 115 App 内处理后用重导。

以上限制只针对 JAV 的 `/import-jobs`。非 JAV 普通视频使用 `/video-imports`，支持 `source_cid` 目录和 `source_fid` 单文件来源，也完整支持 115 失败文件的重导、删除和重命名。其有效文件在入库前必须通过**源 pickcode** 完成 64 MiB 预算内的技术元数据探测并得到非空 `video_info`；探测失败不创建条目、不搬运。详见 [Videos 域导入说明](../videos/README.md#导入-video-imports)。

## 状态枚举与中文说明

影片导入相关的全部状态码、失败原因、条目分类，以及对应中文说明，统一收口在 `src/common/media_import_status.py`，作为“落库/序列化原始字符串”与“API 展示文案”的唯一权威来源。新增或调整取值时只改这一处，service 与 schema 两侧都引用它，避免散落的字符串字面量与文案漂移。

> 该模块放在零依赖的 `src/common` 叶子层，使 service 与 schema 都能直接引用而不产生循环导入；`describe_*` 对未知取值回退原值，保证脏数据下展示层不崩。

### `DownloadTask.import_status`（下载任务导入阶段状态）

| 取值 | 中文说明 |
|---|---|
| `pending` | 待导入：下载已完成，等待自动导入触发 |
| `running` | 导入中：导入作业正在执行 |
| `completed` | 已导入：媒体文件全部成功入库 |
| `failed` | 导入失败：存在未成功导入的文件 |
| `skipped` | 已跳过：任务未触发导入 |

对外经 `DownloadTaskResource.import_status_label` 暴露中文说明。

### `ImportJob.state`（导入作业执行状态）

| 取值 | 中文说明 |
|---|---|
| `pending` | 待执行：作业已创建，尚未开始扫描导入 |
| `running` | 执行中：正在扫描、抓取元数据或导入文件 |
| `completed` | 已完成：本次导入无失败文件 |
| `failed` | 已失败：存在单文件失败或作业级异常 |

`completed` / `failed` 为终态（`TERMINAL_JOB_STATES`），仅终态作业才允许删除/重命名/重导失败文件。对外经 `state_label` 暴露中文说明。

### `failed_files[].reason`（单条失败原因）

| reason | kind | 中文说明 |
|---|---|---|
| `movie_number_not_found` | file | 未识别番号：无法从文件名/路径解析出影片番号 |
| `metadata_fetch_failed` | file | 元数据抓取失败：从站点获取影片信息失败 |
| `image_download_failed` | file | 图片下载失败：影片封面/海报下载失败 |
| `metadata_upsert_failed` | file | 元数据入库失败：影片信息写入数据库失败 |
| `media_import_failed` | file | 文件导入失败：单个媒体文件搬运/落库异常 |
| `file_too_small` | skipped | 文件过小：低于最小体积阈值，按样本/残片跳过 |
| `source_delete_failed` | warning | 源文件删除失败：媒体已入库，但清理源文件失败（仅告警） |
| `import_job_crashed` | job | 导入流程崩溃：导入过程整体异常中断 |
| `import_job_bootstrap_failed` | job | 作业启动失败：导入作业入队/引导阶段失败 |
| `import_job_interrupted` | job | 导入进程中断：作业未正常结束（孤儿恢复判失败） |
| `retry_sources_missing` | job | 源文件缺失：待重导的源文件均已不存在 |
| `already_indexed_path` | skipped | 已在库中：该文件路径已登记，跳过重复导入 |
| `duplicate_fingerprint` | skipped | 内容重复：库中已存在相同内容的文件，跳过导入 |
| `cloud115_file_censored` | warning | 115 已封禁：文件被 115 标记违规，已登记为失效（拿不到直链也播不了） |
| `cloud115_transfer_failed` | file | 115 搬运失败：云端复制或对账阶段异常 |
| `cloud115_rename_failed` | file | 115 单文件改名未成功或查询到的实际名称不一致 |
| `cloud115_metadata_probe_failed` | file | 115 媒体技术元数据无法在读取预算内完成探测 |
| `cloud115_subtitle_download_failed` | warning | 字幕下载失败：影片已入库，但字幕从 115 下载失败（仅告警） |

对外经 `failed_files[].reason_label` / `failed_files[].kind_label` 暴露中文说明；`kind` 的判定与可操作性见上文[失败条目分类表](#查询导入作业)。

> 注意：catalog 域（`movie_service` / `actor_service`）的单片元数据抓取也用到了 `metadata_fetch_failed` 等同名 reason 字符串，但属于另一套语义，不在本模块管辖范围内。

## 安全约束小结

- 目录浏览/导入/删除/重命名/重导一律限定在 `[media_import].browse_roots` 白名单根目录（含子树）内，解析后越界一律 `403`；白名单外的符号链接条目自动跳过。
- 删除/重命名/重导仅允许在作业终态、对 `kind=file` 的单文件失败项执行，且写操作前强制 `is_file` 校验，杜绝对目录的破坏性操作。
- 本地目录导入按来源设置 mutex 防重；115 导入不设置库级 mutex。两类任务入队失败都会回收对应 task_run。

## 业务态恢复

- `media_directory_import` 已注册启动恢复 handler（`MediaImportJobService.recover_orphaned_jobs`）：进程重启后把无存活 owner 进程、也无活跃后台线程的 `pending`/`running` 手动导入作业复位为 `failed` 并补记 `import_job_interrupted`，对应 `BackgroundTaskRun` 一并回收。
- 仍存活的 owner 进程（按 `task_run.owner_pid` 判定）或活跃后台线程的作业会被跳过，避免误杀正在运行的导入。
- 关联下载任务的导入由下载侧 `download_task_import` handler 负责，二者不重复处理。
