# Image Search Sessions

## 资源说明

以图搜图能力采用搜索会话资源建模。客户端上传一张查询图片，服务端同步完成 JoyTag 向量化、执行 LanceDB 检索，并直接返回第一页结果。

所有时间字段都由后端按当前运行环境时区转换后返回，格式为不带时区后缀的本地时间字符串。

当前实现特征：

- 搜索会话是临时资源，过期时间由 `image_search.session_ttl_seconds` 控制
- 创建会话时不会进入异步处理中状态，当前 `status` 实际固定为 `ready`
- 所有接口都需要 `Authorization: Bearer <token>`
- 查询图片使用 `multipart/form-data` 上传，不使用 JSON body
- 搜索结果中的图片字段沿用通用 `ImageResource`，返回签名后的图片 URL

## 前置条件与数据准备

API 本身只负责“上传查询图 -> 检索已索引缩略图”。要让搜索返回结果，必须先有可检索的缩略图向量数据。

当前数据链路如下：

1. 导入媒体后生成 `Media`
2. 定时任务或单次命令 `generate-media-thumbnails` 为媒体生成 `MediaThumbnail`
3. 新建缩略图默认 `joytag_index_status = PENDING`
4. 定时任务或单次命令 `index-image-search-thumbnails` 读取待索引缩略图，调用 JoyTag 生成向量，并写入 LanceDB
5. 定时任务或单次命令 `optimize-image-search-index` 负责压缩数据和建立/维护索引，但不是“可以搜索”的前置条件

补充说明：

- 如果 LanceDB 表尚未建立，或还没有任何已索引缩略图，创建搜索会话仍然会成功，但 `items` 会是空数组
- 删除媒体时，服务会 best-effort 删除对应 `media_id` 的向量记录
- 当前索引任务只扫描 `joytag_index_status = PENDING` 的缩略图

相关命令可参考 [../deployment/commands.md](../deployment/commands.md)，容器部署与 JoyTag 模型准备可参考 [../deployment/docker.md](../deployment/docker.md)。

## 资源模型

搜索结果页资源：

```json
{
  "session_id": "d0e808f39d6e460a856d1dbf0f3f6232",
  "status": "ready",
  "page_size": 20,
  "next_cursor": "eyJ2IjoxLCJvZmZzZXQiOjIwfQ",
  "expires_at": "2026-03-08T10:10:00",
  "items": [
    {
      "thumbnail_id": 123,
      "media_id": 456,
      "movie_id": 789,
      "movie_number": "ABC-001",
      "offset_seconds": 120,
      "score": 0.91,
      "image": {
        "id": 10,
        "origin": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "small": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "medium": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "large": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=..."
      }
    }
  ]
}
```

会话详情资源：

```json
{
  "session_id": "d0e808f39d6e460a856d1dbf0f3f6232",
  "status": "ready",
  "page_size": 20,
  "next_cursor": "eyJ2IjoxLCJvZmZzZXQiOjIwfQ",
  "expires_at": "2026-03-08T10:10:00"
}
```

字段说明：

- `session_id`: 搜索会话 ID
- `status`: 当前固定为 `ready`
- `page_size`: 会话创建时确定的每页结果数
- `next_cursor`: 下一页游标，没有下一页时为 `null`
- `expires_at`: 会话过期时间
- `items[].thumbnail_id`: 命中的缩略图 ID
- `items[].media_id`: 缩略图所属媒体 ID
- `items[].movie_id`: 缩略图所属影片 ID
- `items[].movie_number`: 影片番号
- `items[].offset_seconds`: 缩略图在媒体中的秒级偏移
- `items[].score`: 归一化后的相似度分数
- `items[].image`: 缩略图图片资源

## 端点列表总览

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/image-search/sessions` | 创建搜索会话并返回第一页结果 |
| `GET` | `/image-search/sessions/{session_id}` | 获取会话元信息 |
| `GET` | `/image-search/sessions/{session_id}/results` | 按游标读取会话结果页 |

## POST /image-search/sessions

### Purpose

上传查询图片，创建一个新的搜索会话，并立即返回第一页结果。

### Auth

需要 Bearer Token。

### Request Content-Type

`multipart/form-data`

### Form Fields

- `file`: 必填，查询图片文件
- `page_size`: 可选，正整数；未传时使用 `image_search.default_page_size`
- `movie_ids`: 可选，逗号分隔整数串，例如 `1,2,3`
- `exclude_movie_ids`: 可选，逗号分隔整数串，例如 `10,11`
- `score_threshold`: 可选，`0-1` 之间的浮点数

约束与行为：

- `page_size` 必须大于 `0`，且不能超过 `image_search.max_page_size`
- `movie_ids` 和 `exclude_movie_ids` 为空串时按未传处理
- `movie_ids` 和 `exclude_movie_ids` 会在创建时写入会话，并作用于后续所有翻页请求
- `score_threshold` 是会话级过滤条件，会在结果资源组装阶段应用
- 上传文件为空时返回 `400`

### Success Responses

- `200 OK`: 创建成功，返回会话信息和第一页结果

### Error Responses

- `400 Bad Request`: 空文件、CSV 整数格式非法、`page_size` 非法、`score_threshold` 越界、图片字节无效
- `401 Unauthorized`: 未认证
- `422 Unprocessable Entity`: 缺少 `file` 或表单字段类型不合法

### Example Request

```bash
curl -X POST \
  http://localhost:8000/image-search/sessions \
  -H "Authorization: Bearer <token>" \
  -F "file=@/absolute/path/query.png" \
  -F "page_size=20" \
  -F "movie_ids=1,2,3" \
  -F "exclude_movie_ids=10,11" \
  -F "score_threshold=0.7"
```

### Example Response

```json
{
  "session_id": "d0e808f39d6e460a856d1dbf0f3f6232",
  "status": "ready",
  "page_size": 20,
  "next_cursor": "eyJ2IjoxLCJvZmZzZXQiOjIwfQ",
  "expires_at": "2026-03-08T10:10:00",
  "items": [
    {
      "thumbnail_id": 123,
      "media_id": 456,
      "movie_id": 789,
      "movie_number": "ABC-001",
      "offset_seconds": 120,
      "score": 0.91,
      "image": {
        "id": 10,
        "origin": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "small": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "medium": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=...",
        "large": "/files/images/movies/ABC-001/media/fingerprint/thumbnails/120.webp?expires=1700000900&signature=..."
      }
    }
  ]
}
```

## GET /image-search/sessions/{session_id}

### Purpose

读取搜索会话元信息，不返回结果项。

### Auth

需要 Bearer Token。

### Path Params

- `session_id`: 搜索会话 ID

### Success Responses

- `200 OK`: 返回会话元信息

### Error Responses

- `401 Unauthorized`: 未认证
- `404 Not Found`: 会话不存在或已过期

### Behavior

- 服务端在读取前会先清理已过期会话
- `next_cursor` 表示该会话“最近一次结果分页后”保存下来的下一页游标
- 如果客户端尚未翻页，`next_cursor` 通常等于创建会话时返回的下一页游标

### Example Request

```http
GET /image-search/sessions/d0e808f39d6e460a856d1dbf0f3f6232
Authorization: Bearer <token>
```

## GET /image-search/sessions/{session_id}/results

### Purpose

按游标读取会话结果页。

### Auth

需要 Bearer Token。

### Path Params

- `session_id`: 搜索会话 ID

### Query Params

- `cursor`: 可选，上一页返回的 `next_cursor`

### Success Responses

- `200 OK`: 返回结果页

### Error Responses

- `400 Bad Request`: `cursor` 非法
- `401 Unauthorized`: 未认证
- `404 Not Found`: 会话不存在或已过期

### Behavior

- 首次查询通常直接使用 `POST /image-search/sessions` 返回的第一页结果
- 后续翻页使用上一页的 `next_cursor`
- 当 `cursor` 不传时，服务会从头重新读取第一页
- 每次读取结果页后，服务端都会把新的 `next_cursor` 回写到会话记录

### Example Request

```http
GET /image-search/sessions/d0e808f39d6e460a856d1dbf0f3f6232/results?cursor=eyJ2IjoxLCJvZmZzZXQiOjIwfQ
Authorization: Bearer <token>
```

## 分页、筛选与分数规则

### 分页规则

- 搜索页大小在创建会话时确定，后续翻页接口不会重新接受 `page_size`
- `cursor` 是服务端生成的偏移游标，客户端应视为不透明字符串
- `next_cursor = null` 表示没有下一页

### 过滤规则

- `movie_ids` 表示仅在指定影片集合中检索
- `exclude_movie_ids` 表示从结果中排除指定影片
- 两个过滤条件会同时下推到 LanceDB 查询表达式
- 搜索结果组装阶段还会额外跳过 `media.valid = False` 的媒体
- `score_threshold` 在命中结果转资源时生效，不是 LanceDB 原生距离阈值

### 扫描规则

- 为了尽量凑满一页结果，服务不会只抓取精确 `page_size` 条 LanceDB 命中
- 实际扫描批次大小为 `max(page_size, image_search.search_scan_batch_size)`
- 命中结果在分页后还会继续经过“无效媒体过滤”和“阈值过滤”，因此某些情况下可能继续向后扫描

### 分数规则

- LanceDB 检索度量固定使用 `cosine`
- API 不直接暴露 LanceDB 原始 `_distance`
- 返回给客户端的 `score` 按 `1 - distance / 2` 映射到 `0-1` 区间，并做边界裁剪
- `score` 越大表示越相似

## 配置项与调度任务

当前默认配置如下：

```toml
[image_search]
inference_base_url = "http://joytag-infer:8001"
inference_timeout_seconds = 30
inference_connect_timeout_seconds = 3
inference_api_key = ""
inference_batch_size = 16
session_ttl_seconds = 600
default_page_size = 20
max_page_size = 100
search_scan_batch_size = 100

[lancedb]
uri = "/data/indexes/image-search"
table_name = "media_thumbnail_vectors"
vector_dtype = "float16"
distance_metric = "cosine"
vector_index_type = "ivf_rq"

[scheduler]
image_search_index_cron = "*/10 * * * *"
image_search_optimize_cron = "0 */6 * * *"
```

当前实现说明：

- JoyTag 推理由独立 `joytag-infer` 服务负责，主服务只保存检索会话、索引状态和 LanceDB
- 媒体导入后会先产生 `media` 记录；后续媒体巡检任务会继续修正 `media.valid`，并为缺失记录补齐 `video_info`
- 嵌入维度不是在 API 文档层硬编码的常量；主服务会通过远端运行时状态读取维度，并要求与 LanceDB 表中的向量列维度一致
- 远端推理服务可按部署镜像选择 CPU、OpenVINO 或 CUDA 后端
- LanceDB 向量列类型固定要求为 `float16`
- LanceDB 距离度量固定为 `cosine`
- LanceDB 默认会维护 `movie_id`、`thumbnail_id`、`media_id` 的标量索引
- 向量索引优先尝试 `IVF_RQ`；当前环境或 LanceDB 版本不支持时，会回退到 `IVF_PQ`
- 新数据写入后即可参与搜索；`optimize` 主要用于压缩数据并建立或维护向量索引，不阻塞基础搜索能力

## 当前实现边界

- 当前公开接口只有：
  - `POST /image-search/sessions`
  - `GET /image-search/sessions/{session_id}`
  - `GET /image-search/sessions/{session_id}/results`
- 当前主服务只支持通过远端 `joytag-infer` 服务完成查询图向量化
- 搜索会话状态当前没有细分生命周期，接口层只返回 `ready`
- 结果分页依赖会话中持久化保存的查询向量、筛选条件和 `next_cursor`
- 接口文档描述的是当前实现，不包含模型可插拔、多账号隔离或异步搜索任务等扩展语义
