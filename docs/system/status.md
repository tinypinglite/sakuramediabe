# Status

## 资源说明

状态资源用于返回首页仪表盘需要的全局统计、图像检索链路健康信息和外部元数据站点联通测试结果。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

## 资源模型

```json
{
  "backend_version": "v1.2.3",
  "actors": {
    "female_total": 12,
    "female_subscribed": 8
  },
  "movies": {
    "total": 120,
    "subscribed": 35,
    "playable": 88
  },
  "media_files": {
    "total": 156,
    "total_size_bytes": 9876543210
  },
  "media_libraries": {
    "total": 3
  }
}
```

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/status` | 获取系统汇总统计 |
| `GET` | `/status/image-search` | 获取 JoyTag/LanceDB 运行状态与索引计数 |
| `GET` | `/status/metadata-providers/{provider}/test` | 测试闭源 Provider 提供的 JavDB/DMM 外部站点实际可用性 |

## `GET /status`

需要 Bearer Token。

成功响应：

- `200 OK`: 返回统计资源对象

字段口径：

- `backend_version`: 后端发布版本号（由镜像构建时注入的 `SAKURAMEDIA_BACKEND_VERSION` 提供；本地默认 `dev-local`）
- `actors.female_total`: `Actor.gender == 1` 的总数
- `actors.female_subscribed`: `Actor.gender == 1` 且 `Actor.is_subscribed == true` 的总数
- `movies.total`: `Movie` 总数
- `movies.subscribed`: `Movie.is_subscribed == true` 的总数
- `movies.playable`: `Media.valid == true` 的媒体所关联的去重影片数量
- `media_files.total`: `Media` 总行数
- `media_files.total_size_bytes`: 所有 `Media.file_size_bytes` 的求和（空库为 `0`）
- `media_libraries.total`: `MediaLibrary` 总数

## `GET /status/image-search`

需要 Bearer Token。

成功响应：

- `200 OK`: 始终返回检测结果；子系统异常通过 `healthy=false` 与 `error` 字段表达

示例响应：

```json
{
  "healthy": true,
  "checked_at": "2026-03-16T07:30:00",
  "joytag": {
    "healthy": true,
    "endpoint": "http://joytag-infer:8001",
    "backend": "openvino",
    "execution_provider": "OpenVINOExecutionProvider",
    "used_device": "GPU",
    "available_devices": ["OpenVINOExecutionProvider", "CPUExecutionProvider"],
    "device_full_name": null,
    "model_file": "/data/lib/joytag/model_vit_768.onnx",
    "model_name": "joytag-onnxruntime",
    "vector_size": 768,
    "image_size": 448,
    "probe_latency_ms": 42,
    "error": null
  },
  "lancedb": {
    "healthy": true,
    "uri": "/data/indexes/image-search",
    "table_name": "media_thumbnail_vectors",
    "table_exists": true,
    "row_count": 15320,
    "vector_size": 768,
    "vector_dtype": "halffloat",
    "has_vector_index": true,
    "error": null
  },
  "indexing": {
    "pending_thumbnails": 23,
    "failed_thumbnails": 2,
    "success_thumbnails": 15295
  }
}
```

字段口径：

- `healthy`: `joytag.healthy && lancedb.healthy`
- `checked_at`: 本次检测时间
- `joytag.endpoint`: 当前主服务访问的远端推理服务地址
- `joytag.backend`: 推理服务当前使用的部署后端，例如 `cpu`、`openvino`、`cuda`
- `joytag.execution_provider`: ONNX Runtime 实际启用的 Execution Provider
- `joytag.used_device`: 实际执行推理的设备标识，例如 `CPU`、`GPU`、`CUDA`；`openvino + GPU` 启动时会先做一次真实推理校验，不会仅按配置值回显
- `joytag.available_devices`: 推理服务当前可见的 Provider 列表
- `joytag.device_full_name`: 可选设备全名；当前默认可能为空
- `joytag.probe_latency_ms`: 本次真实推理探测耗时（毫秒）
- `joytag.error`: JoyTag 初始化或推理失败时的错误信息
- `lancedb.table_exists`: 向量表是否存在；`false` 不视为异常
- `lancedb.row_count`: 当前向量表行数；仅在 `table_exists=true` 时有值
- `lancedb.has_vector_index`: 是否存在向量索引；仅在 `table_exists=true` 时有值
- `lancedb.error`: LanceDB 诊断失败时的错误信息
- `indexing.pending_thumbnails`: `MediaThumbnail.joytag_index_status == PENDING` 数量
- `indexing.failed_thumbnails`: `MediaThumbnail.joytag_index_status == FAILED` 数量
- `indexing.success_thumbnails`: `MediaThumbnail.joytag_index_status == SUCCESS` 数量

## `GET /status/metadata-providers/{provider}/test`

需要 Bearer Token。

`provider` 仅支持：

- `javdb`
- `dmm`

该接口用于联调和排障，会真实发起外部网络请求，不会写数据库、不会创建任务记录。当前固定使用测试番号 `SSNI-888`，不接收请求参数。

成功响应：

- `200 OK`: 始终返回检测结果；外部站点异常通过 `healthy=false` 与 `error` 字段表达
- `422 invalid_metadata_provider`: `provider` 不是 `javdb` 或 `dmm`

JavDB 示例响应：

```json
{
  "healthy": true,
  "checked_at": "2026-04-26T14:30:00",
  "provider": "javdb",
  "movie_number": "SSNI-888",
  "elapsed_ms": 842,
  "error": null,
  "javdb_id": "abc123",
  "title": "SSNI-888",
  "actors_count": 2,
  "tags_count": 12,
  "description_length": null,
  "description_excerpt": null
}
```

DMM 示例响应：

```json
{
  "healthy": true,
  "checked_at": "2026-04-26T14:30:00",
  "provider": "dmm",
  "movie_number": "SSNI-888",
  "elapsed_ms": 1350,
  "error": null,
  "javdb_id": null,
  "title": null,
  "actors_count": null,
  "tags_count": null,
  "description_length": 180,
  "description_excerpt": "简介前 120 个字符"
}
```

失败示例响应：

```json
{
  "healthy": false,
  "checked_at": "2026-04-26T14:30:00",
  "provider": "javdb",
  "movie_number": "SSNI-888",
  "elapsed_ms": 10015,
  "error": {
    "type": "metadata_request_error",
    "message": "metadata request failed: GET https://...",
    "method": "GET",
    "url": "https://...",
    "resource": null,
    "lookup_value": null
  },
  "javdb_id": null,
  "title": null,
  "actors_count": null,
  "tags_count": null,
  "description_length": null,
  "description_excerpt": null
}
```

字段口径：

- `healthy`: 是否成功按固定番号拉取并解析到目标数据
- `checked_at`: 本次检测时间
- `provider`: 当前测试的外部站点，值为 `javdb` 或 `dmm`
- `movie_number`: 固定测试番号，当前为 `SSNI-888`
- `elapsed_ms`: 本次检测耗时（毫秒）
- `error.type`: `metadata_request_error`、`metadata_not_found` 或 `unexpected_error`
- `javdb.*`: JavDB 成功时返回的详情摘要；站点请求由闭源 Provider 提供，JavDB 默认不走 `settings.metadata.proxy`
- `dmm.*`: DMM 成功时返回的简介摘要；站点请求由闭源 Provider 提供，代理沿用统一的 `settings.metadata.proxy` 配置
- `metadata_license_error`: 闭源 Provider 未激活、授权过期或授权状态不可用；可先调用 `/metadata-provider-license/status` 查看状态
