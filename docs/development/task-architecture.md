# 统一任务架构改造设计（Task Architecture）

> 状态：**设计定稿，分波实施中**（当前进度见文末「波次与验收」）。
> 本文是任务体系重构的单一事实来源：触发、队列、进度、状态、重试、恢复、操作协议全部以此为准。
> 实施期间旧机制与新内核并存，以各波次的迁移清单为界。

## 1. 背景与动机

现状存在三套互不引用的任务/状态体系（APS `JobDefinition` 注册表、`BackgroundTaskRun`、
`ResourceTaskState` + 各领域作业模型），生命周期记账散落在每个任务自己手里，导致大量
"架构缺位后在使用点补功能"的补丁：

- run_id 靠 ContextVar 隐式传递，线程池不继承，全仓只有缩略图一处正确 wrap；
- 重试预算三个任务三种载体：硬编码常量（缩略图 `THUMBNAIL_MAX_RETRIES=2`）、
  配置 + `exhausted`（订阅搜索）、TEXT 列上对 `extra.terminal` 做子串匹配（desc_sync）；
  翻译任务则完全没有预算，失败后每轮 cron 无限重试；
- 同一张 `resource_task_state` 表上并存两种互不兼容的重置语义（缩略图改字段保留行、
  订阅搜索直接 DELETE 行）；
- 手动触发跑在 Web 进程 daemon 线程（长任务占 uvicorn 线程）；`MovieTaskService`
  的三个单片任务直接在 HTTP 请求线程同步执行、无 mutex，会与 APS 全量批跑并发；
- 崩溃恢复靠 `owner_pid` + 启动时 `force=True`，多容器共享 DB 时 PID 语义不成立；
- `last_task_run_id` 是裸整数，活动清理服务删 task_run 后悬空；
- job 注册表（24 个）与资源任务注册表（6 个）key 空间不重合
  （`subscribed_movie_auto_download` vs `subscribed_movie_search`），
  恢复注册表（`BUSINESS_RECOVERY_HANDLERS`）又是第三份硬编码，插件够不着。

改造原则：**统一的是执行内核与操作协议，不强行统一领域数据模型**
（ImportJob / DownloadTask / MediaRapidUploadBatch 保留各自模型，只接入统一协议）。

## 2. 目标架构总览

```
触发层（只做入队）                 执行层（worker 进程）
┌────────────────────┐            ┌─────────────────────────────────┐
│ APScheduler（纯闹钟）│──┐        │ worker 池（并发道 default/import/│
│ API 手动触发        │──┼─→ 队列 →│ rapid_upload），SKIP LOCKED 领取 │
│ 启动恢复 / 内部联动  │──┘  (PG)  │ + lease 心跳                     │
└────────────────────┘            │   └→ Runner：逐资源生命周期记账   │
                                  │       ├ 域钩子 setup_run()       │
     TaskRun（队列 + 运行记录）     │       ├ 域钩子 process_one()     │
     ResourceTaskAttempt（不可变历史）│     └ 错误分类→attempt→投影→重试 │
     ResourceTaskState（最新投影）  └─────────────────────────────────┘
```

四层数据模型：

| 层 | 载体 | 职责 |
|---|---|---|
| TaskDefinition | 代码注册表（合并 JOB_REGISTRY + TASK_REGISTRY + 恢复表） | 静态定义：cron、触发策略、executor、重试策略、actions、恢复钩子 |
| TaskRun | `background_task_run`（扩列） | 一次执行 = 一行；pending 行即队列元素；进度/结果/互斥 |
| ResourceTaskAttempt | `resource_task_attempt`（新表） | 每个资源每次尝试一行，不可变，带 error_code/retryable |
| ResourceTaskState | `resource_task_state`（扩列） | 每资源最新投影，由内核维护，供列表/计数快速查询 |

## 3. 表结构（Wave 0 落地）

### 3.1 `background_task_run` 扩列

| 新列 | 类型 | 用途 |
|---|---|---|
| `params` | TEXT(JSON), null | 运行参数（如 `{"only_ids": [...]}`），手动子集运行/重试的载体 |
| `scheduled_at` | TIMESTAMP, null | 计划执行时间；worker 只领取 `scheduled_at <= now` 的 pending 行 |
| `lease_expires_at` | TIMESTAMP, null | 租约到期时间；running 且租约过期 = 可回收（取代 owner_pid 判活） |

新索引：`(state, scheduled_at)`（队列领取路径）。

### 3.2 `resource_task_attempt` 新表

```
id, task_key, resource_type, resource_id,
task_run_id  FK -> background_task_run (SET NULL),
attempt_no   INT      -- 该资源生命周期内的第 N 次尝试
trigger_type CHAR
state        CHAR     -- running / succeeded / failed / deferred / aborted
error_code   CHAR null, error_message TEXT null, retryable BOOL null,
started_at, finished_at, created_at, updated_at
索引：(task_key, resource_type, resource_id), task_run_id
```

尝试历史只增不改（终态回写同一行的 state/finished_at/错误字段后不再变更）。
重试/重置**不再清空历史**。

### 3.3 `resource_task_state` 扩列

| 新列 | 类型 | 用途 |
|---|---|---|
| `next_retry_at` | TIMESTAMP, null | 精确重试调度；候选查询叠加 `next_retry_at <= now`，重试节奏与 cron 解耦 |
| `error_code` | CHAR(64), null | 结构化错误码，取代解析 `last_error` 字符串与 `extra.terminal` 子串匹配 |
| `retry_round` | INT default 0 | 重试轮次；`reset_retry_budget` 递增轮次重开预算，attempt_count 保留终身累计 |
| `last_attempt_id` | FK -> resource_task_attempt (SET NULL) | 最新一次尝试 |

存量列变更：`last_task_run_id` 由裸 IntegerField 改为真外键（ON DELETE SET NULL），
迁移时先清悬空值再补约束。新索引：`(task_key, state, next_retry_at)`。

**注意：状态词汇的数据迁移不在 Wave 0 做。**存量 `extra.terminal=true` 的行改写为
`failed_terminal` 必须与对应 executor 的读写侧同波切换（Wave 2 按任务逐个迁），
否则现有候选查询/重置接口会把这些行漏判。Wave 0 只做纯加列，全程向后兼容。

## 4. 状态机

### 4.1 TaskRun

`pending → running → completed | failed | cancelled`

- `pending` 行即队列元素；scheduled 触发**带 coalesce 语义**：同 task_key 已有
  pending/running 行时不再入队（等价现状 `coalesce=True, max_instances=1`，积压即丢弃）；
  manual 触发按 definition 声明选择 409 或排队。
- 领取：`UPDATE ... SET state='running', lease_expires_at=now+lease WHERE id = (
  SELECT id FROM background_task_run WHERE state='pending' AND scheduled_at <= now
  ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)`，执行中定期续租。
- 回收：running 且 `lease_expires_at < now` → 标 failed + 触发领域恢复钩子。
  `owner_pid` 判活与启动 `force=True` 路径随之退役。

### 4.2 资源投影（ResourceTaskState.state）

`pending / running / succeeded / failed_retryable / failed_terminal / exhausted`

- `failed_retryable`：可重试失败，`next_retry_at` 生效；
- `failed_terminal`：确定性失败（如 DMM 确认无此番号、缺内容指纹），永不自动重试，
  取代 `extra.terminal`；
- `exhausted`：预算耗尽（重试轮内 attempt 次数达上限），可经 `reset_retry_budget` 捞回；
- attempt 的 `deferred`（如缩略图 `ThumbnailDeferred`）不改变投影状态、不耗预算。

任务级词汇统一为 `completed`，资源级统一为 `succeeded`（现状即如此，保持；
前后端枚举在 Wave 4 一次对齐补 `exhausted` 等缺失项）。

## 5. 执行内核

### 5.1 executor 三分类

按全量盘点（23 个内置 job + 8 个非 APS task_key），任务形态分四类 A/B/C/D，
映射到三种 executor：

```python
executor: ResourceExecutor   # A 逐资源循环：内核接管记账/重试/attempt/并发
        | UnitExecutor       # C 外部对账：单位（client/board/period）逐个成败，不进投影表
        | BatchExecutor      # B 全量重算 + D 维护清理：整任务成败，内核只管 run 级记账
```

### 5.2 ResourceExecutor 契约

```python
@dataclass(frozen=True)
class ResourceExecutor:
    resource_type: str
    retry: RetryPolicy                    # max_attempts / backoff / 默认 retryable / 豁免钩子
    select_candidates: Callable[..., Query]   # 只写域条件（如 Movie.desc == ""）
    setup_run: Callable[[RunContext], Any] | None   # 每 run 一次，产出跨资源共享上下文
    process_one: Callable[[RunContext, Resource], ItemResult]
    per_run_concurrency: ConcurrencySpec | None     # 任务内并发由内核开线程、显式传 ctx
    pacing: PacingPolicy | None                     # 跨资源限速（如 115 的 10–30s 匀速）
```

内核对每个资源固定执行：

```
行级领取（UPDATE state='running' WHERE state != 'running'，天然与批跑互斥）
→ 写 attempt(running, run_id, attempt_no)
→ process_one(ctx, resource)
→ 成功        → attempt=succeeded；投影=succeeded
→ TaskItemError(code, retryable):
    retryable 且轮内未超预算 → failed_retryable + next_retry_at=backoff(n)
    retryable=False          → failed_terminal
    超预算                   → exhausted
→ Deferred    → attempt=deferred；投影不变、不耗预算
→ TaskAbortError → 当前资源 attempt=aborted、投影回 pending；整个 run 中断
（同事务更新 attempt + 投影；进度按节流窗口上报，不逐条写 system_event）
```

盘点逼出来的三个必备语义，均为一等公民：

- **`setup_run`**：`media_file_scan` 的 115 remote_index（库级枚举失败时整库资源
  跳过不判定）、翻译任务的 prompt 预载与 enabled 门、desc_sync 的 provider 会话、
  热评的跨页去重集——没有它，一半 A 形态任务塞不进纯 process_one 模型。
- **`TaskAbortError`**：统一翻译任务的 abort（当前资源回 pending + 中断）与
  desc_sync 的 DMM 熔断（现状是"静默 return False 不写状态"，改为显式 abort）。
- **`deferred` 第三态**：缩略图 `ThumbnailDeferred` 的归宿。

候选查询 = 域条件（executor 提供）∩ 状态条件（内核提供：
`state IN (pending, failed_retryable) AND (next_retry_at IS NULL OR next_retry_at <= now)`，
manual 子集运行时再叠 `only_ids`）。`movie_interaction_sync` 这类无法下推 SQL 的
分层筛选，`select_candidates` 允许返回 Python 侧过滤器，内核只强制记账。

### 5.3 并发模型

| 并发道 | 容量 | 覆盖 | 复刻自 |
|---|---|---|---|
| default | 4 | 全部 APS 形态任务 | `ThreadPoolExecutor(4)`，04:00/05:00 多任务同发不劣化 |
| import | 2 | 导入族四个 service | `DownloadImportRunner`（顺带消灭 ImportJob/VideoImportJob 共用 `_futures` id 空间的缺陷） |
| rapid_upload | 2 | 秒传 | `MediaRapidUploadRunner` |

worker 循环收在 aps 进程（实施后更名 worker 进程语义）。API 进程只做：插 pending 行、
读状态、SSE。领取轮询间隔 1–2s，远小于现有分钟级 cron 粒度，
6 个分钟级 job（`cloud115_offline_sync` 每 1 分钟最敏感）延迟不劣化。

ContextVar 传 run_id 的整套机制（`TASK_RUN_CONTEXT` / `wrap_current_task_run_context`）
随内核接管任务内并发而删除——ctx 显式传参。

## 6. 恢复模型（两层）

- **lease 过期自动回收**：run 标 failed + 该 run 名下 running 的投影行回 failed_retryable
  （attempt 补 aborted 终态）。覆盖现状 5 个"纯状态复位"型恢复钩子
  （4 个 movie_* + 缩略图），这 5 个领域钩子删除。
- **领域恢复钩子**（成为 TaskDefinition 字段，吞并 `start/recovery.py` 硬编码表，
  插件同权可用）保留给 4 个真有业务决策的：
  - `media_directory_import`：删半截 ImportJob + DownloadTask 退回 pending 重排；
  - `media_rapid_upload`：`remote_uploaded` 中间态分流 + 补发完成通知；
  - `download_task_import` / `video_directory_import`：写 failed_files 明细 + 级联。
- 顺带修复现状漏洞：恢复钩子按"存在孤儿领域行"触发，而非"本次回收到该 task_key
  的 task_run"（现状 Phase 2 在 task_run 已终态但领域行半截时不会触发）。

## 7. 操作协议

- 资源任务统一入口 `POST /system/resource-task-actions`：
  `{task_key, action, resource_ids}` → `{task_run_id, accepted, skipped:[{id, reason}]}`。
  action 枚举：`retry_now`（建带 only_ids 的 TaskRun）/ `rerun` / `reset_retry_budget`
  （retry_round+1，不清历史）/ `cancel`（预留）。
- `available_actions` 由后端按投影状态 + definition 计算返回，与 action 枚举同名，
  前端只按枚举渲染（教训：`allow_reset` 只给能力位不给协议，前端解析后弃用）。
- 领域作业（导入 retry/rerun、订阅搜索、下载任务）**保留领域 endpoint**，只统一响应
  形状（accepted/skipped+reason/task_run_id/available_actions）——导入 retry 的
  kind 过滤、browse_roots 白名单、cloud115 分叉塞不进通用 handler。
- 两种 reset 语义统一为"保留历史、重开预算"；订阅搜索的 DELETE 行重置废弃。
- `MovieTaskService` 三个单片任务（单片翻译/互动同步/热度重算）退役，
  改为统一 action（`retry_now` + only_ids），从此与全量批跑行级互斥。

## 8. 全量任务映射

### 8.1 APS 注册表 23 个内置 job（+ 插件）

| task_key | 形态 | 目标 executor | 说明 |
|---|---|---|---|
| movie_desc_sync | A | ResourceExecutor(movie) | DMM 熔断改 TaskAbort；terminal 判定改 error_code |
| movie_desc_translation | A | ResourceExecutor(movie) | abort 语义入内核；**新增预算**（现状无限重试） |
| movie_title_translation | A | ResourceExecutor(movie) | 同上 |
| movie_interaction_sync | A | ResourceExecutor(movie) | 候选筛选留 Python 侧过滤器钩子 |
| media_thumbnail_generation | A | ResourceExecutor(media) | deferred 三态；双泳道并发交 per_run_concurrency |
| subscribed_movie_auto_download | A | ResourceExecutor(movie) | **与 `subscribed_movie_search` 合并为同一 task_key**；新片豁免走 RetryPolicy 钩子；115 匀速走 pacing |
| actor_subscription_sync | A | ResourceExecutor(actor) | 迁移后获得资源级状态（现状仅领域时间戳列） |
| media_file_scan | A | ResourceExecutor(media) | remote_index 走 setup_run；库级枚举失败=整库跳过 |
| image_search_index | A/B | BatchExecutor（暂） | 状态在领域列 `joytag_index_status`；补"重索 FAILED"action，暂不迁投影 |
| download_task_auto_import | A(入队器) | BatchExecutor | 扇出的 `download_task_import` 变为带 params 的 TaskRun |
| ranking_sync | C | UnitExecutor(board×period) | JavdbAuthError 特判保留 |
| hot_review_sync | C | UnitExecutor(period) | 跨页去重集走 setup_run |
| download_task_sync | C | UnitExecutor(qb client) | cron */5，coalesce 语义必须保留 |
| cloud115_offline_sync | C | UnitExecutor(115 client) | cron */1；串行导入+匀速+等待子作业，保持整任务互斥 |
| movie_heat_update | B | BatchExecutor | 原样迁 |
| movie_collection_sync | B | BatchExecutor | 原样迁 |
| movie_similarity_recompute | B | BatchExecutor | 蓝绿 collection + alias 切换，不可拆事务单元 |
| moment_recommendation_generate | B | BatchExecutor | 原样迁 |
| daily_recommendation_generate | B | BatchExecutor | 原样迁 |
| download_small_file_cleanup | D | BatchExecutor | 原样迁 |
| image_search_optimize | D | BatchExecutor | 原样迁 |
| gfriends_filetree_refresh | D | BatchExecutor | 需补进程间缓存失效（api 进程单例陈旧问题） |
| activity_record_cleanup | D | BatchExecutor | 保留策略需与 attempt/FK 对齐（决策 #8） |
| cloud115_cookies_keepalive | D | BatchExecutor | 原样迁 |

### 8.2 非 APS task_key（8 个）

| task_key | 现状 | 目标 |
|---|---|---|
| media_directory_import（本地/115） | api 进程 `DownloadImportRunner` 池 | import 道；115 触发补 mutex |
| video_directory_import（本地/115） | 同上（共用 id 空间缺陷） | import 道 |
| download_task_import | api/aps 进程均可能执行，无 mutex | import 道，带 params 的 TaskRun |
| media_rapid_upload | 独立池，无 mutex | rapid_upload 道，补 mutex |
| movie_desc_translation / movie_interaction_sync / movie_heat_update（MovieTaskService 单片） | HTTP 请求线程同步执行，无 mutex | 退役，改统一 action |
| movie_subtitle_fetch | 历史残留 | 迁移中清理 |

CLI 长任务（migrate-jav-layout / migrate-plot-layout / backfill-* / scan-media-files）
维持"逐条原子提交点 + 重跑收敛"模式不迁入队列；`scan-media-files` CLI 裸跑与 APS
并发的缺口在 Wave 2 迁 `media_file_scan` 时以行级领取自然解决。

## 9. 波次计划与验收

### Wave 0：表结构（纯加列，向后兼容）✅ 本波
- [x] `background_task_run` + params/scheduled_at/lease_expires_at + (state, scheduled_at) 索引
- [x] 新表 `resource_task_attempt`
- [x] `resource_task_state` + next_retry_at/error_code/retry_round/last_attempt_id；
      `last_task_run_id` 外键化（清悬空 + SET NULL）
- [x] 迁移版本 `20260729_01_add_task_queue_and_attempts` + `test_migrations` / `test_initdb` 护栏
- 验收：存量库迁移幂等；现有全部行为无变化（纯加列）；悬空 last_task_run_id 清零

### Wave 1：队列内核 ✅
- [x] `TaskQueueService`：入队（mutex 唯一索引实现 coalesce/冲突）、SKIP LOCKED 领取、
      续租、租约过期回收（`src/service/system/task_queue_service.py`）
- [x] `TaskWorker`：APS 进程内 4 领取线程 + housekeeper（续租/租约回收/遗留行
      pid 判活回收，均联动业务恢复钩子）（`src/scheduler/worker.py`）
- [x] APScheduler 改 enqueue-only；手动触发入队（202 语义），Web 进程不再起执行线程
- [x] `recover_interrupted_task_runs` 默认排除队列托管行（scheduled_at 非空），
      队列 pending 行跨进程重启存活
- [x] 切换迁移 `20260729_02` 清空 run/event 历史（FK SET NULL 兜底）
- [x] `gfriends` 跨进程缓存失效：resolve 侧按 mtime 节流检查，disk cache 被
      APS 进程重写后 API 进程自动重新加载
- 未做（后续波次）：import / rapid_upload 并发道（Wave 3 随导入族迁移启用）；
  bootstrap 引导任务仍走进程内 date job（与队列 mutex 同名互斥，行为不冲突）
- 验收：`test_aps` 新增 4 条接线护栏；队列语义 8 条护栏（tests/service/test_task_queue_service.py）

### Wave 2：A 形态任务迁 ResourceExecutor（进行中）
- [x] 内核落地：`ResourceTaskRunner` / `ResourceTaskLedger` / `RetryPolicy` /
      `TaskItemError`(error_code+retryable) / `TaskItemDeferred` / `TaskAbortError`
      （`src/service/system/resource_task_runner.py`；7 条生命周期护栏）。
      本轮预算 = 投影 `attempt_count`（reset 归零、retry_round+1），终身次数 = attempt 表行数
- [x] `movie_desc_sync` 迁移完成（首个任务，pathfinder）：
      候选走内核状态条件；DMM 熔断由静默跳过改为显式 `TaskAbortError`；
      `sync_movie_desc` 公共入口（upsert 链路复用）改 Ledger 单资源记账，
      终态判定 `state == failed_terminal` 取代 extra.terminal 子串匹配；
      存量状态行由 `20260729_03` 清空重建；counts schema 与筛选白名单
      已加入 failed_retryable / failed_terminal
- [x] `movie_desc_translation` / `movie_title_translation` 迁移完成：
      共享基类整体上内核；prompt 缺失从 abort 异常改 setup_run 前置中止；
      上游限流重试耗尽 → TaskAbortError（当前影片回 pending 不耗预算）；
      业务性失败首次获得预算（30min 起步指数退避、5 次/轮，决策 #9）；
      单影片手动入口同步改 Ledger 记账；存量状态行由 `20260729_04` 清空
- [x] `movie_interaction_sync` 迁移完成：时间分层判定保留 Python 侧（不用内核
      默认状态条件，状态排除语义等价合入）；`last_succeeded_at` 记忆经 `20260729_05`
      保留（决策 #11 例外），未成功行删除重 seed；JavDB 番号消失判 failed_terminal。
      顺带修内核语义：成功收口本轮（attempt_count 归零），周期任务的历史成功
      不再吃掉失败预算
- [x] `media_thumbnail_generation` 迁移完成：内核落地任务内并发
      （`ResourceTaskSpec.concurrency`，worker 线程自建连接、ctx 显式传参，
      `wrap_current_task_run_context` ContextVar 链路退役）；双泳道 = cloud115
      串行先行 + 本地并发两次 Runner 执行；缺指纹判 failed_terminal、生成失败
      2 次/轮后 exhausted、源未就绪 deferred；重置接口对齐 kernel 语义
      （接受三种失败态、重开预算 retry_round+1、不再动 extra）；
      `20260729_06` 清空并按 MediaThumbnail 存在性播种 succeeded
- 剩余顺序：订阅搜索（合并 key）→ 媒体巡检 → 演员同步
- **存量状态策略：切换即清空该 task_key 的 `resource_task_state` 行（清空重建，
  不做语义映射迁移），仅两个例外**（决策 #11）：
  - `movie_interaction_sync`：必须保留 `(resource_id, last_succeeded_at)`——
    该时间戳在领域数据无副本，清空 = 重放全库 seed = 30w 次 JavDB 请求；
  - `media_thumbnail_generation`：清空前用一条 `INSERT...SELECT` 从 `MediaThumbnail`
    存在性播种 succeeded 行，避免全库空转（领域护栏 `task_service.py`
    的 thumbnails_already_exist 跳过保证清空本身安全）。
  - `subscribed_movie_search` 清空的用户可见后果已确认接受：exhausted（已放弃）
    订阅复活、各多吃一轮预算后重新耗尽。
- 删除：ContextVar 链路、TEXT 子串匹配、各任务手写状态 SQL、5 个纯复位恢复钩子
- 验收：重试节奏按 next_retry_at；翻译任务预算生效；单资源与批跑互斥

### Wave 3：领域作业接协议
- 导入族迁 import 道（`DownloadImportRunner` 退役）、115/秒传补 mutex、
  `MovieTaskService` 退役、领域恢复钩子进 TaskDefinition、
  订阅页 rerun 动作 + 重导继承 `download_task`
- 验收：`import_failed` 订阅行可操作；重导后订阅关联不断链

### Wave 4：前端与协议收口
- action 驱动渲染、`exhausted` 展示、资源级 SSE（按 run 聚合节流）、
  插件契约 host_api_version 2（可声明 ResourceExecutor 与恢复钩子）、
  前后端状态枚举一次对齐

## 10. 决策记录

| # | 决策 | 结论 |
|---|---|---|
| 1 | 手动触发改入队（202 + SSE），不再当场起线程 | 采用；前端体验从"立刻 running"变"pending→running" |
| 2 | Wave 0 不建 attempt 即止损 vs 同步建表 | 同步建表（本文 3.2），但止损项（外键化、reset 不清历史）同波落地 |
| 3 | available_actions 全由后端返回 | 采用；前端仅保留纯展示层状态 |
| 4 | retry_now 互斥粒度 | TaskRun 层 task+trigger 命名空间（manual 不与 scheduled 抢锁）；资源层靠行级 state 转移 |
| 5 | MovieTaskService 同步响应语义变化 | 接受，改 202 |
| 6 | 导入族执行位置迁 worker 进程 | 迁；api 进程重启不再打断导入 |
| 7 | gfriends 进程内单例陈旧 | Wave 1 补磁盘缓存 mtime 失效检查 |
| 8 | activity_record_cleanup 与新台账保留策略 | FK 一律 SET NULL 兜底；attempt 表按 run 保留期同步清理，细则 Wave 1 定 |
| 9 | 翻译任务预算值 | Wave 2 迁移时定（建议 max_attempts=5/轮，退避 1h 起） |
| 10 | 订阅搜索"新片不计次" | RetryPolicy 增加豁免钩子（按资源属性跳过预算），不做特例 |
| 11 | 存量任务状态的处置 | **清空重建**：Wave 2 各任务切换时直接 DELETE 该 task_key 的状态行，不做 `extra.terminal → failed_terminal` 等语义映射（历史可弃，记忆按"能否从领域数据重建"判断）。例外：`movie_interaction_sync` 保留 last_succeeded_at；缩略图清前播种 succeeded。领域作业表（DownloadTask/ImportJob/VideoImportJob/秒传）**不属于可清范围**。run/event 历史随 Wave 1 清空 |
