# Downloads

## 资源说明

下载域负责对接 Torznab 索引器、qBittorrent 与 115 离线下载，并管理本地可查询的下载状态。

下载入口按 `DownloadClient.kind` 区分两种：

- `qbittorrent`：独立部署的本地下载器，绑定 local 媒体库
- `cloud115`：挂在 cloud115 媒体库上的 115 离线下载能力（无独立部署，凭据在媒体库 `backend_config` 中）

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

- Torznab 索引器负责“搜索候选资源”
- qBittorrent 负责“实际下载”
- `DownloadTask` 是本地镜像数据，由“提交下载”或“同步任务”流程写入
- API 不提供 `DownloadTask` 的通用创建、更新、详情接口；只提供查询、实时状态与受控操作
- 删除下载任务会同时移除本地镜像；是否删除 qBittorrent 中的下载文件由调用方显式确认

## 边界说明

- 索引器配置继续使用系统级接口 `/indexer-settings`
- 下载客户端配置使用下载域接口 `/download-clients`
- 搜索结果 `DownloadCandidate` 为临时资源，不落库
- 提交下载使用命令式接口 `POST /download-requests`
- 提交前一律经过种子内容闸门（见下「种子内容闸门」），手动提交与自动下载共用同一道校验
- 定时任务可自动搜索“已订阅但缺失媒体且没有下载记录”的影片，并自动提交下载

## 设计目标

- 保持依赖方向为 `api -> service -> model`
- 让索引器配置与 qBittorrent 客户端配置解耦
- 让搜索、提交下载、任务同步、媒体导入分成独立流程
- 允许一套索引器配置服务多个 `DownloadClient`
- 允许多个 `DownloadClient` 绑定不同媒体库
- 支持后续增加定时同步与自动导入，而不破坏 API 边界
- 支持后续增加自动搜索订阅影片资源，而不新增额外下载 API

## 内部定时任务

系统包含一个内部调度任务，用于自动搜索并提交“已订阅缺失影片”的下载请求。

行为约定：

- 仅处理 `is_subscribed = true` 的影片
- 仅处理不存在有效 `Media` 且**不存在活跃 `DownloadTask`** 的影片（判定的是活跃而非存在，见下「死种判定」）
- 仅处理**尚未放弃**的影片（`state != exhausted`，见下「查询次数与放弃」）
- 使用 Torznab 协议搜索 PT 与 BT 候选资源
- 选种为「过滤 → 打分取最高」两步，与 `downloads.preferred_client_kinds` 无关（见下）
- 复用 `POST /download-requests` 对应的 service 提交下载，不新增 API

选种过滤（依次执行，无任何豁免）：

1. 无磁力也无种子链接的候选剔除
2. 大小不在 1G–40G 区间的剔除
3. `info_hash` 命中该影片选种黑名单的剔除（见下「选种黑名单」；`info_hash` 未知的照常放行）
4. `seeders = 0` 的剔除

选种打分：`score = size_bytes + 中字加成（2G）`，取最高分。大小主导；中字加成等价于
「中字版最多容忍比无中字版小 2G 仍然优先」。分数相同（同一个种子被多个索引器同时返回）时按
`(indexer_name, title)` 兜底，保证选种确定性——黑名单排除依赖「同一批候选每轮选出同一个」。

提交阶段还会经过种子内容闸门（见下「种子内容闸门」），并在同一拉 .torrent 的解析里补全
torrent-only 候选的 `info_hash` 做死种黑名单比对。被内容闸门或死种黑名单拒绝时**在本轮就地换种**：
把该候选加入本轮拒绝集合后重选次优，最多换 `MAX_REJECTED_CANDIDATES`（5）次；全部用完
仍无可提交候选，按「本轮没找到资源」处理并计入查询次数。拒绝集合按
`(indexer_name, title, size_bytes)` 标识候选而不是 `info_hash`——PT 索引器可能连
`infohash` 和磁力都不给，候选身份要等提交阶段解析 .torrent 才确定。**拒绝记录只在本轮内有效**，
跨轮不记忆：下一轮该影片会重新拉一次这些种子文件再拒一次。

说明：

- 下载器选择与选种解耦：赢者候选所属索引器绑定了哪些下载入口，就按 `preferred_client_kinds`
  在绑定集合内挑第一个命中的 kind。PT 索引器禁绑 115（`pt_indexer_cloud115_binding_unsupported`），
  因此 PT 候选必然落到本地 qBittorrent
- 候选不再按 4K / PT 分池，也不再设做种人数下限门槛（仅剔除 0 做种）
- 候选全灭时日志会带各过滤环节的击杀计数（`no_source / size_filtered / blacklist_filtered / seeders_filtered`），
  用于回答「订阅了为什么一直不下」
- `downloads` 配置节的生效档位是 `restart_scheduler`，改完 `preferred_client_kinds` 需重启 aps 进程才对本任务生效

依赖前提：

- 已通过 `/indexer-settings` 配置可用的 `Indexer`
- 每个 `Indexer` 必须绑定一个 `DownloadClient`
- `DownloadClient` 必须绑定一个可用的 `MediaLibrary`

另有两个内部定时任务：

- `sync-download-tasks`（每 5 分钟）：qB 对账——状态归一化、判死、`download_started_at` 维护、
  ghost 任务反向清理
- `cleanup-qb-stalled-tasks`（每天凌晨 1 点）：停滞 / 慢速任务自动清理（删种 + 删文件 + 拉黑），
  详见「停滞 / 慢速任务自动清理」一节

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
  该影片同样回到候选池——这条链路早已存在，与死种判定互补。**仅限非死态行**：死态行
  （failed / abandoned / stalled_dead）对反向对账豁免（见「选种黑名单」与「停滞 / 慢速任务自动清理」）。

#### 停滞 / 慢速任务自动清理（`cleanup-qb-stalled-tasks`）

每天凌晨 1 点（`qbittorrent_stalled_cleanup_cron`，早于订阅自动下载 02:30，删完当天就能换种重下）
清理 qB 中长期没有进展的种子：**删种 + 连带删除已下载文件 + 本地行落 `stalled_dead` 拉黑**。
配置：`qbittorrent_stalled_cleanup_enabled`（默认开）、`qbittorrent_stalled_cleanup_hours`（默认 24）。

判定（命中即删，`QBStalledCleanupService`，只碰系统标签种子）：

- 归一化状态 ∈ `{stalled, downloading}` 且未完成（progress < 1）
- **且 `DownloadTask.download_started_at` 距今 ≥ 阈值**——该字段由对账维护：进入活跃下载态
  （stalled / downloading）写当前时刻，离开（暂停 / 完成 / 做种 / 排队 / 失败）即清空

为什么不用 qB 的 `added_on` 计时：qB 接口没有"种子开始下载的时刻"字段，直接用添加时刻会把
排队时长算进"下载时长"。一次性订阅 2000 部 + qB 并发位占满时，队尾种子排队几天必然轮不上，
按添加时刻计时会把这批排队种子全部误删并永久拉黑。

**永不清理**：`queuedDL`（排队是用户配置并发数的正常现象）、`pausedDL` / `stoppedDL`
（用户显式暂停）、`error` / `missingFiles`（对账即映射为 `failed` 立即拉黑，不进清理流程）。

存量行首次部署时 `download_started_at` 为空：先由对账起算，**首轮只写入不删除**，避免误杀。

删除后的拉黑与 `stalled_dead` 判死共用同一套台账；区别是清理删掉了 qB 侧种子和已下载文件，
后续对账不会再看到它、状态不会流回，黑名单是**永久**的（要重试需手动删任务，见选种黑名单一节）。

### 种子内容闸门

`POST /download-requests` 对应的 `DownloadRequestService.create_request` 在分派下载器**之前**，
先拉取候选的 `.torrent` 并解析文件列表，内容不可导入时直接拒绝提交。实现在
`src/service/transfers/downloads/guards/torrent_content_guard.py`。qB 与 115 共用这个入口，自动下载与手动提交
也都走它，因此这是唯一需要维护的拦截点。

判据：统计种子里的**合格视频文件数** = 后缀命中 `SUPPORTED_VIDEO_EXTENSIONS` **且**体积不低于
`media.allowed_min_video_file_size` 的文件数量。

- `0` → 拒绝，典型是蓝光/DVD 原盘（正片是单个 `.iso`）
- 合格视频解析出的**不同番号数 > 1** → 拒绝，典型是演员合集包

番号判定逐文件复用导入侧 `parse_movie_number_from_scan_path`（只看父目录 + 文件名最后两段），
并对种子内相对路径垫一个虚拟根段，使解析口径与落盘后的绝对路径完全对齐——无根目录、番号只在
目录名（如 `STARS-001/part.mkv`）的合集包也能拦下。去重直接按解析输出原串比较、不做
`normalize_movie_number` 折叠——一本道 `072625_001` 与加勒比 `072625-001` 是同串形态的两部
不同影片，折叠会误并。单部影片的多分卷（VR / FC2 的 A、B、C）都解析出同一个番号，不受影响；
**0 个番号放行**但打 warning 日志——多文件种子导入阶段大概率 `MOVIE_NUMBER_NOT_FOUND` 失败，
单文件种子可靠父目录番号兜底正常导入。

判据直接复用导入侧的两条约束，因此闸门拒绝的一定是导入侧也会丢弃的（反过来不成立：导入侧
还要求能从路径解析出番号，见 `media_source_scanner.parse_movie_number_from_scan_path`，闸门对
解析不出番号的资源是放行的）。方向上只会漏、不会误拒；将来支持新容器格式或调整体积阈值，闸门自动跟随，
**不需要维护任何格式关键词表**。

为什么必须读文件列表（SSIS-037 生产实测，38 个候选 / 去重后 33 个种子）：

- **标题判不了**：原盘那条标题是 `SSIS-037 三上悠亜に逆痴●されたい？されたいでしょ？`，与
  6.82G 的普通压制版**逐字相同**，没有任何 ISO / BDMV / 原盘字样
- **体积判不了**，两个方向都有反例：20.97G 那条是单个合法的 4K `mp4`；而 DVD 原盘通常只有
  4–8G，比一半的压制版还小
- **索引器分类只覆盖 PT**：M-Team 有 `AV(有碼)/Blu-Ray Censored`(100431) 这类精确分类，
  但 sukebei / knaben 全部候选共用同一个分类值，没有原盘粒度
- **最小体积那条约束不可省**：公开站种子普遍夹带 40–50MB 的广告 `mp4`，实测 `total_files` 达
  32/34/36/45 的四个种子，合格视频数都是 1

两类错误码，区别只体现在给 HTTP 调用方的语义上；**自动下载对两者一视同仁地换种**：

- `download_candidate_content_rejected`（422）：内容确定不合格
- `download_candidate_content_unverifiable`（502）：拿不到或解析不了种子文件

不可校验之所以也换种而不是中止该影片：中止要走 `consumes_budget=False`，而它会回滚重试计数且
**永不判 exhausted**（`ResourceTaskRunner._finish_failed`），稳定复现的坏候选会让这部影片每轮
重来、永远放弃不掉。换种则会在候选耗尽时正常落到「本轮没找到资源」并消耗查询次数，可收敛。
索引器整体故障不会走到这里——那种情况 `search_candidates` 会先失败并按 `indexer_search_failed`
处理（那才是真正的基础设施故障，不消耗次数）。

拉取策略：`FETCH_ATTEMPTS = 2`、`FETCH_TIMEOUT_SECONDS = 20`。Torznab 聚合器（如 Jackett）
的 `/dl/` 下载端点要回源到上游站点，偶发超时是常态，重试即可恢复；生产实测串行重试下
33/33 全部可得（并发压测时会出现瞬时失败，因此闸门刻意逐个候选串行校验）。次数与超时压得紧，
是因为单候选的最坏耗时会被换种次数放大（最坏 5 × 2 × 20s）。

**日志与 `ApiError.details` 里绝不能出现原始下载地址。** Torznab 服务返回的下载地址通常自带
鉴权参数（Jackett 形如 `http://host:9117/dl/<indexer>/?jackett_apikey=<KEY>&...`），apikey 就在
query 里；httpx 的异常字符串也会内嵌完整 URL，而 `details` 会被 API 层原样返回给调用方。
因此 URL 一律经 `_redact_url` 去 query，异常一律经 `_describe_fetch_error` 压成「类型名 / HTTP 状态码」。

**只有磁力链的候选一律判为不可校验。** 磁力本身不含文件列表，要拿到只能走 BEP-9 从 swarm 换
metadata，生产实测 6 条冷门磁力在 120 秒内只换到 1 条（耗时 67 秒），做不了提交前的同步闸门。
链接分流**按内容而非字段名**，与 `QBittorrentClient.add_candidate` / `resolve_magnet_from_links`
保持一致：索引器会把磁力塞进 `torrent_url` 字段，照字段名处理会拿 `magnet:` 当 HTTP 地址去 GET。

#### 选种黑名单

重新查资源时必须排除该影片已判死的种子，否则选种排序是确定性的，会把同一个死种反复选中。

- 黑名单 = 该番号下所有已判死 `DownloadTask` 的 `info_hash`
- 候选侧已知的 `info_hash`（torznab infohash / 磁力链）在选种阶段纯内存比对，**零网络请求**；
  torrent-only 候选在提交阶段由内容闸门解析 `.torrent` 后确认，命中的死种按「不合格候选」换下一个
- 排除后无候选 = 本轮没找到资源，正常计入查询次数
- **黑名单是永久的，重置查询状态不放开它。** `info_hash` 是内容寻址的——同一个 hash 就是同一个
  swarm，换个索引器它照样是死的；用户重置后真正想要的是找一个**别的**种子，而黑名单本来就不挡这个。
  确实要重试某个具体种子时，**手动删除该下载任务**（UI 删任务会同步删 qB 侧与本地台账行）——
  反向对账 `_prune_ghost_tasks` 对死态行（failed / abandoned / stalled_dead）**豁免**，仅凭在 qB 里
  删掉种子不会解除黑名单；豁免是停滞清理的闭环前提：否则删完种子下轮对账就抹掉黑名单，
  第二天自动下载又把同一死种拉回来。

**种子身份从哪来**：

1. torznab 响应里的 `<torznab:attr name="infohash">` —— 索引器直接给的
2. 磁力链里的 `xt=urn:btih:`

前两条都是纯字符串处理，选种阶段不为此发起网络请求。两者都拿不到时 `info_hash` 为空串——
**空串表示「本次没能廉价地确定身份」，不表示「没有这个种子」**，所以选种时照常放行而不是跳过。

真正补身份的地方在**提交阶段的内容闸门**：torrent-only 候选本来就要下载 `.torrent` 校验内容，
顺手把 `torrent_info.info_hash()` 解出来。解析后命中该影片死种黑名单的候选抛
`download_candidate_dead`，自动下载把它当作「不合格候选」换下一个；全部换完仍无可提交候选时
才落到 `no_candidate` 正常消耗查询预算。这样既不为整个候选池逐个拉 `.torrent`（每影片最多
`MAX_REJECTED_CANDIDATES`（5）个候选、每候选最多 `FETCH_ATTEMPTS`（2）次），也堵住了
「PT 源只给 torrent 链接导致死种被反复重提交」的闭环。

`info_hash` 的规范化统一走 `src/service/transfers/shared/common.py` 的 `canonicalize_btih()`（hex/Base32 →
40 位小写 hex）。它放在 transfers 公共模块而不是某个下载器模块里：选种、115 离线对账、任务删除、索引器
候选四条链路都要用，且必须是同一个实现，否则「这两个是不是同一个种子」在不同链路上会给出不同答案。
实测同一个种子在 knaben（大写 hex）和 sukebei（小写 hex）上会收敛到同一个字符串。

#### 查询次数与放弃

避免老片长期没有资源却年复一年地查索引器。状态落在 `ResourceTaskState`
（`task_key=subscribed_movie_auto_download`，与定时任务同 key；kernel 逐资源记账，
见 [task-architecture.md](../development/task-architecture.md)），不额外建表。

调度是每天一轮（`subscribed_movie_auto_download_cron` 默认 `30 2 * * *`），所以「每轮都查」就等于
「每天查一次」。规则只有两档：

| 档 | 判定 | 节奏 |
|---|---|---|
| 新片 | `release_date` 在 `subscription_search_fresh_days`（默认 90 天）内，**含未来日期** | 每轮都查，**不计次数，永不放弃** |
| 老片 | 其余，含 `release_date` 为空的（无法证明它新） | 每轮都查，累计 `attempt_count`，满 `subscription_search_stale_attempt_limit`（默认 3）置 `exhausted` |

即老片**连查 3 天后放弃**。这里刻意不做逐次退避：老片的种子可得性基本是静态的，把 3 次摊到几十天
并不比连查 3 天多抓到什么；真要捞重新做种的片子得是月/年尺度的重扫，那靠订阅管理页的「重置全部
已放弃」手动触发，而不是让每部影片都背一套阶梯参数。

「还要不要查」在写入时就落进了 `state`，调度器的候选集仍是一条纯 SQL：排除
`exhausted` / `failed_terminal` / `running`，`failed_retryable` 看 `next_retry_at`——本任务退避
为零、写入即到期，等价「下一轮照查」。「查过没找到」带 `error_code=no_candidate_found`，
订阅页据此归入「未找到」档而非「查询失败」；索引器/提交故障声明不消耗查询次数
（`consumes_budget=False`），只落错误信息。本任务不使用 `extra` 列。

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

`DownloadCandidate` 表示一次 Torznab 搜索返回的候选资源，不落库。

```json
{
  "source": "torznab",
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
- `download_started_at`（qb 专用）：进入活跃下载态（stalled / downloading）的时刻，由对账维护；
  排队 / 暂停 / 完成 / 做种时清空。停滞 / 慢速清理按它计时，排队时长不计入

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
- `stalled_dead`（qB 专用：`stalledDL` 且 `last_activity` 超过 `qbittorrent_stalled_abandon_days`；
  也由停滞 / 慢速清理落库。**非粘性**，每轮对账按 qB 实时值重算，peer 回来会自动流回
  `stalled` / `downloading`——清理删掉 qB 侧种子后对账不再上报该 hash，状态即冻结为永久拉黑）

### `import_status` 枚举

- `pending`
- `running`
- `completed`
- `failed`
- `skipped`

## 实时进度与任务控制

客户端先通过 `GET /download-tasks` 分页加载全部历史记录，再以 `GET /download-tasks/stream` 订阅 qBittorrent 的实时状态。两个接口都支持 `client_id` 与 `movie_number` 筛选；SSE 首次为每个匹配下载客户端发送 `snapshot`，随后发送：

`GET /download-tasks` 还支持按下载状态筛选：`download_state` 可重复传多个取值（如
`?download_state=downloading&download_state=stalled`），命中的是并集；未传或传空表示不过滤。
取值集合与下文 `download_state` 枚举一致，非法取值返回 422。

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
- 任务目录**直接 `mkdir`，不再先分页扫描下载根**：提交前已确认本地无同 info_hash 的 `DownloadTask`，而 info_hash 全局唯一，旧实现那次全量翻页必然扫不中（且页数随历史任务累积增长）。只有「上一轮在建目录与登记任务之间中断、留下孤儿目录」这一种情况会撞名，115 回 `errno=20004`，此时才回退分页定位复用。按 errno 精确分派，绝不当作「POST 失败就重试」笼统兜底：webapi 域的裸 HTTP 400 是 WAF 风控签名，由 transport 映射为 `Cloud115RiskControlError` 并触发熔断停批，不会走重名回退
- **订阅自动下载在 cloud115 提交之间随机休息 10~30 秒**（`SUBMIT_REST_*`，与导入侧番号间休息同量级）。必需的原因：每部影片各自新建 SDK client，transport 的匀速闸门与批次计数都会随之归零，跨影片**没有任何机制**在限速——实测提交间隔恒定约 3 秒，订阅积压时就是上百个连续 webapi 请求。休息只在**下一次真要提交前**才等，判定按"上一部是否真的向 115 发过请求"：落到 qB 的提交不碰 115，本地已有同 `(client, info_hash)` 任务而短路返回（`created=false`）的也一个请求都没发，两者都不触发休息；反之提交抛异常时无从判断是否已经打到 115（建目录成功、离线提交失败也是这条路径），而 WAF 一旦触发正是最不能连打的时刻，故一律按"打过 115"记账、下一部先休息
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

根据番号搜索 Torznab 候选资源。

### Auth

需要 Bearer Token。

### Query Params

- `movie_number`: 必填，番号，大小写不敏感
- `indexer_kind`: 可选，`pt` 或 `bt`

### Behavior

- 服务读取 `/indexer-settings` 对应的当前运行时配置
- 当 `movie_number` 以 `FC2` 开头（含 `FC2-PPV-xxxx`）时，调用 Torznab 客户端会仅使用数字部分作为查询词
- 结果为临时数据，不写入数据库
- 每条候选通过 `download_clients` 返回对应索引器绑定的全部可选下载器，`resolved_client_*` 表示按全局偏好预选的默认下载器
- 按“更高做种数优先，其次更大体积优先”排序返回

### Success Responses

- `200 OK`: 返回候选资源数组

### Example Response

```json
[
  {
    "source": "torznab",
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
- `502 Bad Gateway`: Torznab 请求失败

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
    "source": "torznab",
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
