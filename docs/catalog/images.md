# 图片资源（`ImageResource`）

## 资源说明

`ImageResource` 是嵌入在演员、影片和剧照等业务资源里的通用图片对象，本身不是独立 CRUD 资源。

当前主要出现在：

- `ActorResource.profile_image`
- `MovieListItemResource.cover_image`
- `MovieDetailResource.cover_image`
- `MovieDetailResource.thin_cover_image`
- `MovieDetailResource.plot_images[]`

图片文件通过独立文件路由访问：

- `GET /files/images/{file_path:path}`

该路由使用带时效的 query 签名，不走 Bearer Token。

## 资源模型

```json
{
  "id": 10,
  "origin": "/files/images/movies/SONE-210/cover.jpg?expires=1700000900&signature=<signature>",
  "small": "/files/images/movies/SONE-210/cover.jpg?expires=1700000900&signature=<signature>",
  "medium": "/files/images/movies/SONE-210/cover.jpg?expires=1700000900&signature=<signature>",
  "large": "/files/images/movies/SONE-210/cover.jpg?expires=1700000900&signature=<signature>"
}
```

字段说明：

- `id`: 图片记录 ID
- `origin`: 原图访问路径
- `small`: 小图访问路径
- `medium`: 中图访问路径
- `large`: 大图访问路径

当前实现中，这四个字段都指向同一个本地文件，只是为了保持接口结构稳定。

## 路径与访问规则

### 返回值规则

- 接口返回的是带签名的相对 URL 路径，不是裸磁盘路径
- 前端应使用 `base_url + origin` 这类方式拼接成完整访问地址
- `expires` 和 `signature` 由后端动态生成，重启后旧签名可能失效

### 当前存储路径规范

数据库里保存的原始相对路径与磁盘真实相对路径保持一致，当前规则为：

- 演员头像：`actors/{javdb_id}.jpg`
- 影片封面：`movies/{movie_number}/cover.jpg`
- 影片竖封面：`movies/{movie_number}/thin-cover.jpg`
- 影片剧照：`movies/{movie_number}/plots/{index}.jpg`

磁盘根目录来自：

- `settings.media.import_image_root_path`

例如：

```text
storage/import-images/
  actors/
    EM44.jpg
  movies/
    SONE-210/
      cover.jpg
      plots/
        0.jpg
        1.jpg
```

## 文件访问接口

### `GET /files/images/{file_path:path}`

- 鉴权：不需要 Bearer Token
- 安全：需要 `expires` 与 `signature` 两个 query 参数
- 行为：
  - 校验签名是否匹配当前文件路径
  - 校验签名是否过期
  - 只允许访问图片根目录下的文件

示例请求：

```http
GET /files/images/movies/SONE-210/cover.jpg?expires=1700000900&signature=<signature>
```

可能的错误码：

- `file_signature_invalid`
- `file_signature_expired`
- `file_path_invalid`
- `file_not_found`

## 设计备注

- `ImageResource` 是嵌入式资源，不提供 `/images/{id}` 这类单独详情接口
- 当前没有真实多尺寸图片生成逻辑，因此 `origin/small/medium/large` 只是统一接口形态
- 历史旧导入数据如果路径结构不符合当前规范，可能无法通过文件路由访问
