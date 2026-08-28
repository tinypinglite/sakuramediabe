# 媒体导入

媒体导入只接受 provider 定义的 opaque `source_ref`，宿主不接收绝对路径或其它 provider
专属字段。

## 浏览来源

```http
POST /import-sources/browse
```

请求包含 `library_id`、可选 `parent_ref`、`cursor` 和 `limit`。响应中的每个条目都带
`source_ref`、名称、文件/目录类型、大小、修改时间和 `is_video`。宿主只原样转发引用。

## 创建导入

```http
POST /imports
```

请求示例：

```json
{
  "media_kind": "jav",
  "library_id": 1,
  "source_ref": {"id": "provider-defined"},
  "source_disposition": "keep"
}
```

`media_kind` 为 `jav` 或 `video`；视频可选 `collection_id`。`source_disposition` 为
`keep` 或 `delete_after_commit`，其具体语义由 storage provider 执行。

接口返回 `202` 和 TaskRun 标识。后台流程为：

1. storage provider 扫描 opaque 来源并返回 `ImportFile`；
2. 宿主按 `is_video` 和 `media.allowed_min_video_file_size` 做最终准入筛选，不符合条件的文件记为
   skipped，不调用 `stage_import_file`；
3. 每个符合条件的文件以确定性的 operation key 和宿主整理位置调用 `stage_import_file`；
4. stage receipt 先写入通用 TaskRun 参数，然后在数据库事务中写入 Media/VideoItem；
5. 事务提交后调用 `finalize_import`；业务事务失败才调用 `abort_import`。

JAV 导入不要求文件名番号与下载任务番号一致；扫描结果中的每个符合条件的视频文件都按自身解析出的番号导入。
如果本次没有任何文件成功导入且没有失败项，下载任务的导入状态为 `skipped`。

provider 错误以 `provider_*` 错误码返回。宿主不会扫描目录、解析 provider 路径、读取
下载客户端文件或自行搬运媒体。

provider 返回的 `ImportFile.name` 必须是非空单段文件名，不得包含 `/`、`\\`，也不能是 `.` 或 `..`；
不符合时导入直接以 `provider_invalid_response` 失败，宿主不会静默取 basename。
