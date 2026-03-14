# 常见问题

本文整理当前版本最常见的行为说明。

- 所有结论都以当前代码实现为准
- 若你修改过配置，后台任务频率请以实际配置文件为准；本文列的是默认值

## 自动下载相关

### 什么情况下会对影片进行资源搜索并下载？

系统只会在同时满足以下条件时，才会自动搜索影片资源并提交下载：

1. 影片已订阅，也就是 `is_subscribed = true`
2. 影片当前没有任何有效媒体
   - 也就是不存在 `Media.valid = true` 的本地媒体记录
3. 影片当前没有任何下载任务记录
   - 这里是“任何 `DownloadTask` 记录都算”
   - 包括历史失败、已完成、已暂停、已导入失败等情况
   - 只要数据库里已经出现过该影片的 `DownloadTask.movie_number`，就不会再被自动搜索
4. 定时任务运行到该影片时，Jackett 至少返回一个可用候选
5. 候选还要满足基础过滤条件
   - 有 `magnet_url` 或 `torrent_url`
   - `seeders >= 3`
   - 体积在 `1 GiB` 到 `40 GiB` 之间

典型不会触发的情况：

- 影片未订阅
- 已经导入过本地媒体
- 数据库里已经有该影片的任何下载任务记录
- 搜索结果为空
- 搜索结果虽然有候选，但都被过滤掉

触发方式：

- 默认由 APScheduler 定时任务触发
- 也可以手动执行 `python -m src.start.commands aps auto-download-subscribed-movies`

当前实现可参考：

- [src/service/transfers/subscribed_movie_auto_download_service.py](../src/service/transfers/subscribed_movie_auto_download_service.py)
- [src/start/aps.py](../src/start/aps.py)
- [src/start/commands.py](../src/start/commands.py)

### 自动下载时，系统如何选择资源？

结论是：系统会先过滤掉不合格候选，然后按固定优先级排序，只取最优的一个资源提交下载。

基础过滤条件：

- 必须存在 `magnet_url` 或 `torrent_url`
- `seeders >= 3`
- 体积在 `1 GiB` 到 `40 GiB` 之间

优先级规则如下，越靠前优先级越高：

1. 优先选 `4K`
   - 如果候选里存在带 `4K` 标签的资源，只在这批 `4K` 候选里继续排序
   - 如果没有 `4K`，才回退到全部合格候选
2. 在同一候选池里，`PT` 优先于 `BT`
3. 在同一候选池里，`中字` 优先于非中字
4. 然后按做种人数从高到低
5. 再按体积从大到小
6. 最后以 `indexer_name`、`title` 做稳定排序

需要特别注意：

- `4K` 是一级优先级，高于 `PT/BT` 与 `中字`
- `PT` 和 `中字` 只会在同一个候选池内部继续排序时才生效

简单例子：

- 如果一个普通资源做种更多，但另一个资源是 `4K`，系统仍然会优先选择 `4K`
- 如果两个资源都是 `4K`，则先看 `PT/BT`，再看是否中字，最后再看做种和体积

当前实现可参考：

- [src/service/transfers/subscribed_movie_auto_download_service.py](../src/service/transfers/subscribed_movie_auto_download_service.py)

## 后台任务相关

### 后台都会执行哪些任务，默认频率如何？

默认情况下，后台会执行这些 APScheduler 任务：

| 任务 | 作用 | 默认频率 |
|---|---|---|
| 订阅演员影片同步 | 抓取已订阅演员的影片 | 每天 02:00 |
| 已订阅缺失影片自动下载 | 搜索符合条件的订阅影片资源并提交下载 | 每天 02:30 |
| 影片热度重算 | 重算热度字段 | 每天 00:15 |
| 合集影片同步 | 同步合集标记 | 每天 01:00 |
| 下载任务状态同步 | 同步 qBittorrent 任务到本地 `DownloadTask` | 每 1 分钟 |
| 已完成下载自动导入 | 把已完成下载自动交给导入流程 | 每 3 分钟 |
| 媒体缩略图生成 | 为待处理媒体生成缩略图 | 每 5 分钟 |
| 以图搜图索引 | 为待索引缩略图生成向量并写入索引 | 每 10 分钟 |
| 以图搜图索引优化 | 定期压缩或优化向量索引 | 每 6 小时 |

对应的默认 cron 配置分别是：

- `actor_subscription_sync_cron = "0 2 * * *"`
- `subscribed_movie_auto_download_cron = "30 2 * * *"`
- `movie_heat_cron = "15 0 * * *"`
- `movie_collection_sync_cron = "0 1 * * *"`
- `download_task_sync_cron = "* * * * *"`
- `download_task_auto_import_cron = "*/3 * * * *"`
- `media_thumbnail_cron = "*/5 * * * *"`
- `image_search_index_cron = "*/10 * * * *"`
- `image_search_optimize_cron = "0 */6 * * *"`

说明：

- 上述频率都是默认值
- 实际运行频率以配置文件中的 `[scheduler]` 为准
- 任务注册位置在 [src/start/aps.py](../src/start/aps.py)
- 默认配置位置在 [src/config/config.py](../src/config/config.py)
