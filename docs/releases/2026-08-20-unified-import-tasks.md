# 统一媒体导入任务

这是一次破坏性接口与执行模型调整：JAV / 普通视频、不同 provider 导入统一改用
`POST /imports`，进度和结果统一从 `/system/task-runs` 查询。

- 删除 `/import-jobs` 与 `/video-imports` 的作业列表、详情、失败文件重试、重命名和删除接口。
- 删除 `ImportJob` / `VideoImportJob` 运行时模型；旧未完成作业直接放弃，旧表由迁移删除。
- 失败后不再按文件子集续跑；调用方应以原请求整源重试，provider 返回的 ref 负责幂等收敛。
- provider 下载任务统一关联 TaskRun；中断恢复只重置精确关联的下载任务。
- 导入写入由宿主统一加锁；同一 import lane 中的其它任务按 provider 能力并行或串行。

前端需要把四类导入请求切换为 `media_kind + library_id + source_ref + source_disposition` 的统一请求结构，
并使用响应中的 `task_run_id` 轮询任务中心。
