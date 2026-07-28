# Downloads

## 资源说明

下载域负责对接 Jackett、qBittorrent 与 115 离线下载，并管理本地可查询的下载状态。

下载入口按 `DownloadClient.kind` 区分两种：

- `qbittorrent`：独立部署的本地下载器，绑定 local 媒体库
- `cloud115`：挂在 cloud115 媒体库上的 115 离线下载能力（无独立部署，凭据在媒体库 `backend_config` 中）

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
- 仅处理不存在有效 `Media` 且**不存在活跃 `DownloadTask`** 的影片（判定的是活跃而非存在，见下「死种判定」）
- 仅处理**尚未放弃**的影片（`state != exhausted`，见下「查询次数与放弃」）
- 使用 Jackett 搜索 PT 与 BT 候选资源
- 选种时排除该影片已判死的种子（见下「选种黑名单」）
- 选种优先级随 `downloads.preferred_client_kinds` 的**首项**切换两套策略（见下）
- 复用 `POST /download-requests` 对应的 service 提交下载，不新增 API

选种策略：

| 首选下载器 | 候选池 | 池内优先级 | 做种人数门槛 |
|---|---|---|---|
| `qbittorrent`（默认） | 全部可用候选 | `4K > PT > 中字 > seeders > size_bytes` | 全部候选要求 `seeders >= 3` |
| `cloud115` | 先取 BT 候选池，**池为空才回落**到含 PT 的全池 | `4K > 中字 > seeders > size_bytes` | BT 候选不校验；PT 候选仍要求 `seeders >= 3` |

115 优先时 PT 降为兜底的原因：PT 索引器在 `/indexer-settings` 写入侧就被禁止绑定 115 下载入口
（`pt_indexer_cloud115_binding_unsupported`），因此 PT 候选必然落到本地 qBittorrent，与「内容进网盘」的意图相悖。
BT 候选免除做种门槛则是因为 115 离线属云端拉取，本地做种数无参考意义，且部分 Jackett 索引器根本不返回 `seeders`（恒为 0）。

BT/PT 分层在 4K 分层**之外**：115 优先且「PT 有 4K、BT 只有普通版」时仍然选 BT。

说明：

- 这里的下载候选 `4K` 标签仍然来自远端标题或索引器返回信息
- 它和本地 `media.special_tags` 的 `4K` 不是同一套规则；本地媒体侧的 `4K` 来自真实视频流解析
- `downloads` 配置节的生效档位是 `restart_scheduler`，改完 `preferred_client_kinds` 需重启 aps 进程才对本任务生效

依赖前提：

- 已通过 `/indexer-settings` 配置可用的 `Indexer`
- 每个 `Indexer` 必须绑定一个 `DownloadClient`
- `DownloadClient` 必须绑定一个可用的 `MediaLibrary`

#### 死种判定

下载任务进入下列状态即视为**死种**：本地已确知不会再有进展，该影片重新参与资源查询。

| 判定 | 来源 |
|---|---|
| `download_state = failed` | qBittorrent 的 `error` / `missingFiles` |
| `download_state = abandoned` | 115 离线任务超过 `cloud115_offline_abandon_hours`（默认 24h）后的本地放弃 |
| `download_state = stalled_dead` | qB 的 `stalledDL`，且 qB 报告的 `last_activity` 已早于 `qbittorrent_stalled_abandon_days`（默认 7 天） |

要点：

- **判死不删记录。** `DownloadTask` 行本身就是「这部影片试过哪个种子」的台账，选种黑名单直接读它。
- **番号关联一律「裸列 = 裸列」。** `movie.movie_number` 存 provider（JavDB）给出的规范原样
  （分隔符与大小写都是有效信息，不做任何归一化改写）；`download_task.movie_number` 由提交链路
  拷贝同一列（qB 对账重建行时只填空不覆写）。两侧天然同形态，比较时不需要也不能套函数：
  套了就废掉该列索引，订阅管理页那种把此表达式嵌进状态判定的查询会从 1s 退化到 46s
  （3 万订阅影片实测）。人工输入按番号点查是另一条路：`find_movie_by_number` 用
  `UPPER(movie_number)` 等值匹配（函数索引 `movie_movie_number_upper`）+ `_`/`-` 候选互换。
- **判死发生在对账时，不在查询时。** `DownloadSyncService.sync_client` 每轮用
  `resolve_qbittorrent_download_state(raw_state, last_activity)` 把「躺太久的 stalledDL」直接落成
  `stalled_dead`，因此查询侧的判死表达式是纯 `download_state IN (...)`，不带任何时间参数。
  这一点是刻意的：判定依赖 qB 的 `last_activity`，**qB 联系不上时我们不该替它宣布种子死亡**——
  对账不跑 = 状态冻结，正是想要的行为。
- **`last_activity` 是 qB 官方字段**（"Last time (Unix Epoch) when a chunk was downloaded/uploaded"），
  不是本系统自己维护的代理量。qB 对从未有过活动的种子返回的是**添加时刻**而非哨兵值（见
  `serialize_torrent.cpp` 的 `getLastActivityTime`），所以「加进来 N 天一个 chunk 都没收到」也能
  被正确判死，无需额外分支。拿不到可信值（缺失 / 非正 / 在未来）时一律不判死——误判的代价是给还
  活着的种子拉黑并重复提交同一部影片。
- **`stalled_dead` 不粘。** 每轮对账都按 qB 的实时 `last_activity` 重算，peer 回来会自动流回
  `stalled` / `downloading`。这与 `abandoned`（115 的粘性终态）语义不同，故不复用同一取值。
- `stalled` 只可能来自 `stalledDL`：`map_download_state` 已把 `stalledUP` 归到 `seeding`，不会误伤
  做种任务。生产 qB 实测印证了这条的必要性——26 个 `stalledUP` 种子的 `last_activity` 最远已到
  12.7 天前，一旦它们被归到 `stalled`，整批做种任务都会被判死并进黑名单。
- `paused` 永远不判死，那是用户在 qB 里的显式意图。
- `completed` 但 `import_status = failed` 仍算**活跃**：文件已在盘上，该修的是导入而不是重下。
- 种子在 qB 里被手动删除时，`DownloadSyncService._prune_ghost_tasks` 的反向对账会直接删掉本地行，
  该影片同样回到候选池——这条链路早已存在，与死种判定互补。

#### 选种黑名单

重新查资源时必须排除该影片已判死的种子，否则选种排序是确定性的，会把同一个死种反复选中。

- 黑名单 = 该番号下所有已判死 `DownloadTask` 的 `info_hash`
- 候选侧的 `info_hash` **在解析索引器响应时就已确定**，选种阶段是纯内存比对，**零网络请求**
- 排除后无候选 = 本轮没找到资源，正常计入查询次数
- **黑名单是永久的，重置查询状态不放开它。** `info_hash` 是内容寻址的——同一个 hash 就是同一个
  swarm，换个索引器它照样是死的；用户重置后真正想要的是找一个**别的**种子，而黑名单本来就不挡这个。
  确实要重试某个具体种子时，从 qB 里删掉它即可（上面 `_prune_ghost_tasks` 的反向对账会同步删掉
  本地台账行，该 hash 随之离开黑名单）。

**种子身份从哪来**（`JackettClient._resolve_info_hash`），按顺序取第一个能用的，两条都是纯字符串处理：

1. torznab 响应里的 `<torznab:attr name="infohash">` —— 索引器直接给的
2. 磁力链里的 `xt=urn:btih:`

**绝不为了拿 hash 去下载 `.torrent` 文件。** 那是每候选一次网络往返，而选种阶段只是想知道「这个种子
我是不是已经试过了」，不值得。生产实测（knaben + sukebei，4 个番号 56 个候选）第 1 条命中率 **100%**，
第 2 条实际上是给不返回该属性的索引器留的后路。

两者都拿不到时 `info_hash` 为空串——**空串表示「本次没能廉价地确定身份」，不表示「没有这个种子」**，
所以选种时照常放行而不是跳过：它可能压根不是死种，为一个不确定的判断牺牲一个可用候选不划算；真是死种
的话，下一轮它带着 `DownloadTask` 行回来，那时身份就是确定的了。

`info_hash` 的规范化统一走 `src/service/transfers/common.py` 的 `canonicalize_btih()`（hex/Base32 →
40 位小写 hex）。它放在 transfers 公共模块而不是某个下载器模块里：选种、115 离线对账、任务删除、索引器
候选四条链路都要用，且必须是同一个实现，否则「这两个是不是同一个种子」在不同链路上会给出不同答案。
实测同一个种子在 knaben（大写 hex）和 sukebei（小写 hex）上会收敛到同一个字符串。
- **黑名单是永久的，重置查询状态不放开它。** `info_hash` 是内容寻址的——同一个 hash 就是同一个
  swarm，换个索引器它照样是死的；用户重置后真正想要的是找一个**别的**种子，而黑名单本来就不挡这个。
  确实要重试某个具体种子时，从 qB 里删掉它即可（上面 `_prune_ghost_tasks` 的反向对账会同步删掉
  本地台账行，该 hash 随之离开黑名单）。

#### 查询次数与放弃

避免老片长期没有资源却年复一年地查索引器。状态落在 `ResourceTaskState`
（`task_key=subscribed_movie_search`，`resource_type=movie`），不额外建表。

调度是每天一轮（`subscribed_movie_auto_download_cron` 默认 `30 2 * * *`），所以「每轮都查」就等于
「每天查一次」。规则只有两档：

| 档 | 判定 | 节奏 |
|---|---|---|
| 新片 | `release_date` 在 `subscription_search_fresh_days`（默认 90 天）内，**含未来日期** | 每轮都查，**不计次数，永不放弃** |
| 老片 | 其余，含 `release_date` 为空的（无法证明它新） | 每轮都查，累计 `attempt_count`，满 `subscription_search_stale_attempt_limit`（默认 3）置 `exhausted` |

即老片**连查 3 天后放弃**。这里刻意不做逐次退避：老片的种子可得性基本是静态的，把 3 次摊到几十天
并不比连查 3 天多抓到什么；真要捞重新做种的片子得是月/年尺度的重扫，那靠订阅管理页的「重置全部
已放弃」手动触发，而不是让每部影片都背一套阶梯参数。

因为「还要不要查」在写入时就落进了 `state`，读侧不需要任何时间推导——调度器的候选集是一条纯 SQL
（`state IS NULL OR state != 'exhausted'`），没有 Python 侧的到期筛选，也不存冗余的「下次查询时间」。
本任务不使用 `extra` 列。

状态取值：

| state | 含义 |
|---|---|
| `pending` | 等待或本轮没找到资源，下轮继续 |
| `succeeded` | 已提交下载。提交成功也照常记 `attempt_count`——次数的语义是「为这片花了几次搜索」 |
| `failed` | 索引器调用出错。**不记 attempt_count、不动 last_attempted_at**：索引器故障是运维问题，不该消耗该影片的查询次数 |
| `exhausted` | 老片查询次数用尽，只能由用户手动重置 |

**取消订阅不会删这些状态行，因此「未订阅 -> 订阅」的转变必须顺带重置它**（`MovieService` 的单条与
批量订阅入口都做了）。否则一部曾被判 `exhausted` 的影片退订后重新订阅，状态行还是 `exhausted`，
自动下载会直接跳过它，用户侧表现为「重新订阅了却完全没动静」。

提交成功的种子后来判死时，该影片回到候选池并继续消耗次数，跑满同样会被放弃；此时用户能在订阅管理页
看到失败的下载任务历史，据此决定要不要手动重置。

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

`DownloadClient` 表示一个受系统管理的下载入口。`kind = qbittorrent` 时为 qBittorrent 客户端配置；`kind = cloud115` 时创建时只使用 `name` 与 `media_library_id`（qb 连接字段为 `null`），创建后只允许改名。每个 cloud115 媒体库最多对应一个 cloud115 下载入口；换账号或换库必须先解绑索引器，再删除并重建入口。qBittorrent 仍允许同一 local 媒体库配置多个客户端。

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
  "kind": "qbittorrent",
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
  "resolved_client_kind": "qbittorrent",
  "download_clients": [
    {"id": 1, "name": "client-a", "kind": "qbittorrent"},
    {"id": 2, "name": "client-b", "kind": "qbittorrent"}
  ],
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
  "target_ref": null,
  "progress": 0.52,
  "download_state": "downloading",
  "import_status": "pending",
  "created_at": "2026-03-10T08:10:00",
  "updated_at": "2026-03-10T08:20:00"
}
```

说明：

- qb 任务的 `save_path` 为后端可访问路径，应基于 `local_root_path` 计算；cloud115 任务的 `save_path` 是基于完整 canonical hash 的展示路径（如 `sakuramedia_downloads/95a37f09c6d5aac200752f4c334dc9dff91e8cfc`），结构化定位在 `target_ref`
- `target_ref` 是后端结构化落地定位符：qb 为 `null`，cloud115 为 `{"cid": "<hash 独立目录 cid>"}`
- `(client_id, info_hash)` 是任务幂等键
- `movie_number` 可以为空；同步阶段允许先按 `name` 解析，后续再补齐
- `import_status` 只反映本地导入流程，不直接映射 qBittorrent 状态

## 状态约定

### `download_state` 枚举

- `downloading`
- `completed`
- `seeding`
- `paused`
- `failed`
- `stalled`
- `checking`
- `queued`
- `abandoned`（cloud115 专用：离线任务超时后的本地放弃态，不再对账、不再推进度；115 侧任务保留。**粘性终态**）
- `stalled_dead`（qB 专用：`stalledDL` 且 `last_activity` 超过 `qbittorrent_stalled_abandon_days`。
  **非粘性**，每轮对账按 qB 实时值重算，peer 回来会自动流回 `stalled` / `downloading`）

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

进度轮询周期：qBittorrent 由 `[downloads].progress_stream_poll_interval_seconds` 配置，默认 `1.0` 秒（允许 `0.2` 至 `10` 秒，修改后需重启 API）；cloud115 由 `[downloads].cloud115_progress_poll_interval_seconds` 配置，默认 `8.0` 秒（允许 `2` 至 `60` 秒，每轮现读、热生效）。Cloud115 SSE 始终从数据库构造完整快照，仅在存在 `queued/downloading` 任务时拉 115 离线列表补进度；没有活跃任务时零 115 请求。`abandoned` 任务仍保留在快照中，状态变化广播一次后不再请求远端进度。

## cloud115 离线下载

选中 cloud115 kind 的下载入口提交下载时，走 115 离线下载而非 qBittorrent，整体链路：

```
提交（统一磁力）→ 115 服务端离线下载到缓冲目录 → 周期对账 → 完成后 cleanup-source 导入进库
```

### 提交行为

- 所有 115 下载统一走磁力：候选只有 `torrent_url` 时后端先拉取 .torrent 字节、解析 `info_hash`、拼标准磁力再提交；不使用 BT 选文件流程
- BTIH 严格规范化为 40 位小写 hex：40 位 hex 直接转小写，32 位 Base32 解码为 20 bytes 后转 hex，其它格式返回 `422`
- 落地目录为 `sakuramedia_downloads/<完整40位canonical_hash>/`（与库管理目录 `sakuramedia/` 平级的缓冲区），同番号不同资源完全隔离
- 任务幂等键仍为 `(client_id, canonical_hash)`；115 单项提交必须返回唯一、非空且一致的 hash，否则返回 `cloud115_offline_submit_invalid_response` 或 `cloud115_offline_submit_hash_mismatch`
- 115 侧已存在同 hash 任务时，以远端真实 `save_dir_id` 为准，并通过目录面包屑确认它属于当前媒体库的受管下载根；位于用户目录或无法可靠定位时返回 `409 cloud115_offline_task_exists_unmanaged`，不会接管或清理
- 离线月配额耗尽返回 `409 cloud115_offline_quota_exceeded`，不自动降级到其它下载器
- 广告/垃圾小文件不做下载前过滤：导入管线按扩展名白名单分拣，`cleanup-source` 只把命中白名单的视频移进库
- **导入成功后整个任务目录被删除**（进 115 回收站），连同 nfo / 封面 / 种子 / 判定过小的样本等非视频残留一并清掉。仅在本次导入零失败项时执行；有失败项则整个目录保留，供按相对路径重导。这条是必需的：`cleanup-source` 走 `move`，只搬文件不动目录，否则已完成的任务目录会在缓冲区永久累积，而按整个缓冲区导入时扫描要把这些空壳逐个列一遍（实测 158+ 个残留目录会让扫描连打 200 余次 `list_dir` 并触发 WAF 405）

### 周期对账（`cloud115_offline_sync`）

内部调度任务，默认每分钟执行一次（`[scheduler].cloud115_offline_sync_cron`），对每个 cloud115 下载入口：

- 先用本地状态推进 completed 任务的导入与 ImportJob 终态；只有存在 `queued/downloading` 任务时才拉 115 离线列表
- 任务完成（status=2）且待导入 → 按任务创建时间串行消费：触发 cloud115 导入（`cleanup-source`：把视频从缓冲目录直接移动进库）、关联 `ImportJob` 并在本轮等待终态。成功后若还有待导入任务，随机休息 10–30 秒再继续；作业失败或存在失败文件时立即停止本轮，剩余任务留待下一轮
- 自动导入队列在同一下载入口内串行，但不设置媒体库级 mutex；手动 JAV/videos 导入和媒体秒传不会因自动导入而返回库级冲突
- 提交超过 `[downloads].cloud115_offline_abandon_hours`（默认 `24`，最小 `1`）仍处于 queued/downloading → 本地标记 `abandoned` 并发系统通知；**不删除 115 侧任务**，后续不再请求其远端进度。远端 failed 任务保持 failed，不再因超时改为 abandoned
- 没有活跃任务时整轮零请求，不打扰 115
- 容器重启时，关联 Activity 会保留 failed 审计记录；半截 Cloud115 ImportJob 被删除，下载任务回到 `import_status=pending`。启动阶段不访问 115，下一轮周期对账再触发导入；已复制文件与 Media 由现有 SHA/Media 幂等对账收敛
- 可手动单次执行：`uv run python -m src.start.commands aps sync-cloud115-offline-tasks`

### 任务控制差异（相对 qb）

- 暂停/恢复不支持（115 离线无此原语），返回 `422 download_task_action_unsupported`
- 删除任务：非 `abandoned` 任务会先删 115 侧离线任务（`delete_files=true` 时连已下载文件一起删）再删本地镜像；`abandoned` 任务只删本地记录、不动远端（与放弃语义一致）
- 手动触发导入（复用下载任务导入接口）走 cloud115 导入作业链路，不走本地路径导入

配置生效级别：Cloud115 SSE 间隔为热生效；qB SSE 间隔需重启 API；abandon 时长、小文件阈值与 `preferred_client_kinds` 由 APS 消费，修改后需重启 scheduler。

## 端点总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/download-clients` | 获取下载客户端配置列表 |
| `POST` | `/download-clients` | 创建下载客户端配置 |
| `PATCH` | `/download-clients/{client_id}` | 更新下载客户端配置 |
| `DELETE` | `/download-clients/{client_id}` | 删除下载客户端配置 |
| `GET` | `/download-clients/{client_id}/test` | 测试下载入口可用性（qb 测 Web API；cloud115 测媒体库 cookies 存活） |
| `POST` | `/download-clients/{client_id}/storage-test` | 测试下载目录映射与硬链接能力（仅 qbittorrent kind） |
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
- 每条候选通过 `download_clients` 返回对应索引器绑定的全部可选下载器，`resolved_client_*` 表示按全局偏好预选的默认下载器
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
    "resolved_client_kind": "qbittorrent",
    "download_clients": [
      {"id": 1, "name": "client-a", "kind": "qbittorrent"},
      {"id": 2, "name": "client-b", "kind": "qbittorrent"}
    ],
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

- 若请求体包含 `client_id`，优先使用显式指定的目标 `DownloadClient`，但该下载器必须绑定到 `candidate.indexer_name` 对应的索引器
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
- `422 Unprocessable Entity`: 请求体非法，候选资源既无 `magnet_url` 也无 `torrent_url`、`candidate.indexer_name` 未配置，或显式 `client_id` 未绑定到候选索引器
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
