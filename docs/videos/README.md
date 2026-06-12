# Videos 域（非 JAV 视频）

## 定位

`videos` 域用于管理**无番号、无外部元数据**的非 JAV 视频（如个人收藏、国产/国外资源），与 JAV 的 `catalog`（`Movie`）体系完全平行。设计目标是「仅播放 + 整理」：

- 按**标签**（复用 catalog 的通用 `Tag`）与**人物**（`Person`）组织；
- 支持**合集**（`VideoCollection`），成员带 `position`，前端可按序顺序播放；
- 复用现有播放底座（缩略图、播放进度、**时刻** `MediaPoint`、流播放）。

不提供订阅、下载、推荐、相似度、以图搜图等 JAV 专属自动化能力。

## 数据模型

| 模型 | 表 | 说明 |
|---|---|---|
| `VideoItem` | `video_item` | 视频条目（标题/简介/封面/发布时间），1:N 关联 `Media` |
| `Person` | `person` | 人物（姓名/头像/性别），不复用 JAV 的 `Actor` |
| `VideoItemTag` | `video_item_tag` | 条目 ↔ 复用的 `Tag` 多对多 |
| `VideoItemPerson` | `video_item_person` | 条目 ↔ `Person` 多对多 |
| `VideoCollection` | `video_collection` | 合集 |
| `VideoCollectionItem` | `video_collection_item` | 合集成员，`position` 决定顺序播放次序 |

### 播放底座解耦

`Media.movie` 由必填改为可空，并新增可空的 `Media.video_item`；一条 `Media` 归属 `movie`（JAV）或 `video_item`（非 JAV）之一。判定「是否 JAV」统一用 `media.movie_number`（外键原始值）。

- 播放底座（探测、扫描、缩略图生成、时刻增删查、流播放、删除级联）对两类 `Media` 同样生效；非 JAV 缩略图存放在 `videos/{video_item_id}/...` 命名空间下。
- discovery（以图搜图 / 时刻推荐 / 相似度）通过保留对 `Movie` 的 INNER JOIN 或显式 `movie` 非空过滤，**只覆盖 JAV**，不索引非 JAV 媒体。
- 跨域全局列表（全局时刻浏览、失效媒体列表、资源任务展示）改为 LEFT OUTER JOIN，番号相关字段可空，非 JAV 回退展示 `VideoItem.title`。

## 接口

鉴权与 DB 依赖与其它域一致（`db_deps` + `get_current_user`）。

### 视频条目 `/videos`

- `GET /videos`：分页列表，支持 `query`、`tag_id`（可重复）、`person_id`（可重复）、`sort`（`created_at|release_date|title` + `:asc|:desc`）。
- `POST /videos`：创建，body 含 `title`、`summary`、`release_date`、`tag_ids`、`person_ids`。
- `GET /videos/{video_id}`：详情，含 `tags`、`persons`、`media_items`（复用影片媒体资源结构，含播放进度与时刻、签名播放地址）。
- `PATCH /videos/{video_id}`：局部更新；传入 `tag_ids` / `person_ids` 即整体替换关联关系。
- `DELETE /videos/{video_id}`：删除条目及其媒体（复用 `MediaService.delete_media` 清理文件/图片/向量）。

### 人物 `/persons`

- `GET /persons`（分页，支持 `query`/`sort`）、`POST /persons`、`GET/PATCH/DELETE /persons/{person_id}`。
- `PersonResource.video_count` 为关联视频数。

### 合集 `/video-collections`

- `GET /video-collections`、`POST`、`GET/PATCH/DELETE /{collection_id}`。
- `GET /{collection_id}/items`：按 `position` 升序返回成员，供顺序播放。
- `POST /{collection_id}/items`（body `video_item_id`，追加到末尾）、`DELETE /{collection_id}/items/{item_id}`。
- `POST /{collection_id}/items/reorder`（body `ordered_item_ids`）：按给定顺序重写 `position`，要求恰好覆盖全部成员，否则 422。

### 导入 `/video-imports`

- `POST /video-imports`：body 含 `source_path`（目录或单文件）、可选 `library_id`、`tag_ids`、`person_ids`、`collection_id`。
- **就地索引**：不搬运文件，按 `Media.path` 唯一去重；每个视频文件创建一条 `VideoItem`（标题取文件名）+ 一条 `Media`，并按入参关联标签/人物/合集。
- 探测复用 `MediaMetadataProbeService`，内容指纹复用 `src/common/content_fingerprint.py` 的共享算法。

CLI 等价命令见 [../deployment/commands.md](../deployment/commands.md) 的 `import-videos`。
