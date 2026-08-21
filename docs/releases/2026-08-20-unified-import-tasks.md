# 统一媒体导入任务

这是一次破坏性接口与执行模型调整：JAV / 普通视频、本地 / 115 导入统一改用
`POST /imports`，进度和结果统一从 `/system/task-runs` 查询。

- 删除 `/import-jobs` 与 `/video-imports` 的作业列表、详情、失败文件重试、重命名和删除接口。
- 删除 `ImportJob` / `VideoImportJob` 运行时模型；旧未完成作业直接放弃，旧表由迁移删除。
- 失败后不再按文件子集续跑；调用方应以原请求整源重试，现有指纹与 115 fid 对账保证幂等收敛。
- 115 导入固定使用 move，历史 copy 作业不再兼容执行；历史媒体记录和已入库文件不迁移。
- qBittorrent 与 115 下载任务改为关联统一 TaskRun；中断恢复只重置精确关联的下载任务。
- 为避免 move / `cleanup-source` 并发处理重叠目录树，本地导入全局串行，所有 115 导入、自动入库
  和媒体秒传共用一把全局写锁。本地与 115、以及 import lane 中的其它任务仍可并行。

前端需要把四类导入请求切换为 `media_kind + backend + library_id + source` 的统一请求结构，
并使用响应中的 `task_run_id` 轮询任务中心。
