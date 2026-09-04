# 下载

下载能力由媒体 provider bundle 的 `DownloadComponent` 提供。宿主只保存远端任务
投影和 opaque 引用，不解释 provider 配置、路径或文件列表，也不会在 provider 之间
自动切换。

## 客户端

`DownloadClient` 绑定一个媒体库，配置存于 `provider_config`。provider 的
`download_config_fields`（由媒体库 Provider 目录返回）定义可编辑字段、必填项和敏感字段；
敏感字段不会在响应中回显。创建客户端时只提交 `library_id`，Provider 由该媒体库确定。

```http
GET    /download-clients
POST   /download-clients
POST   /download-clients/test
PATCH  /download-clients/{client_id}
DELETE /download-clients/{client_id}
```

客户端的创建和更新会调用 bundle 的 `prepare_client`；provider 不可用、配置无效或
上游失败会转换为结构化 API 错误。

PATCH 不传 `provider_config` 时保留原配置；传对象时更新配置。显式传 `null` 返回 422。
更新对象中缺失的 secret 和 read-only 字段保留原值。

`POST /download-clients/test` 使用当前表单配置执行 `DownloadComponent.test_client`，不保存配置。
请求体包含 `library_id`、`provider_config`，编辑已有客户端时可额外传 `client_id` 以复用未回显的
secret。响应按 `ok`、`warning`、`failed` 返回总体状态，并列出每项检查；warning 或 failed
不会阻止随后保存配置。

## 候选与提交

Torznab 索引器返回候选 `source_uri`。提交时必须指定 `client_id`（自动订阅会使用候选
解析出的客户端），宿主只把 URI 和展示名称包装为 `DownloadSubmission`，调用同一
bundle 的 `submit`。

```http
GET  /download-candidates?movie_number=ABC-001
POST /download-requests
```

provider 返回的 `remote_id` 成为宿主任务幂等键 `(client_id, remote_id)`。

## 任务状态

`DownloadTask` 的远端状态只有 `queued`、`downloading`、`completed`、`failed`，并保留
`progress`。completed 时 provider 返回的完成 ref 仅供后台导入使用，不在普通任务响应中
返回。同步任务调用同一 bundle 的 `list_tasks`，删除任务调用 `delete_task`；宿主不读取 provider 文件清单、
暂停/恢复接口或厂商路径语义。

```http
GET    /download-tasks
DELETE /download-tasks/{task_id}
```

任务番号命中本地影片时，列表项附带 `movie_title`、`movie_cover` 和可选的
`movie_thin_cover`；客户端在竖封面缺失时应回退使用 `movie_cover`。

删除远端文件必须显式传 `delete_files=true&confirm_delete_files=true`。正在导入的任务
不能删除。

## 导入交接

完成任务可通过统一 `/imports` 队列导入。导入服务使用任务所属媒体库的同一 storage
provider，按 `scan_import_source → stage_import_file → 宿主事务 → finalize_import`
顺序执行；宿主事务失败才调用 `abort_import`。所有 stage receipt 写入通用 TaskRun
参数，便于 finalize 重试。
