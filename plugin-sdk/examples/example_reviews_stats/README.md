# 示例：评论与互动数

演示一个插件同时注册两个扩展点：

- `catalog.movie_reviews`：`fetch_reviews(movie_number, page, page_size, sort)` 返回评论；
- `catalog.movie_stats`：`fetch_stats(movie_number)` 返回互动数快照和 `heat`。

静态数据只对 `EXAMPLE-001` 生效。互动数字段必须先在 `PluginMovieStatsField` 里声明，
`values` 里不能出现未声明的 key；`heat` 是宿主唯一的标量排序字段，不能放进 `values`。
修改互动数计算方式时应提升 `heat_version`。

## 检查

```bash
docker run --rm \
  -v "$PWD:/work/example_reviews_stats" \
  tinyping/sakuramediabe:v0.15.7 \
  python /app/cli.py plugins check /work/example_reviews_stats
```

期望输出：`插件 example_reviews_stats 校验通过`。

## 安装试用

打包为 zip（`manifest.json` 和 `__init__.py` 位于根目录），上传安装、启用并重启容器。
在插件设置页把「评论来源」绑定到本插件，然后用 `EXAMPLE-001` 触发插件能力测试或查看影片详情。

完整契约见 [插件开发文档](https://tinypinglite.github.io/sakuramedia/zh-CN/guide/plugin-development)。
