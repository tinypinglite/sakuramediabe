# 示例：静态排行榜来源

演示两件事：

- 用 `discovery.ranking_source` 注册一个榜单来源；
- 注册一个定时任务，执行体调用 `context.sync_ranking_sources()` 让宿主完成榜单同步。

`fetch_numbers(period)` 只返回静态番号 `EXAMPLE-001`、`EXAMPLE-002`、`EXAMPLE-003`；
真实插件应在回调里访问外部站点。榜单的存储、影片详情导入和对外 API 都由宿主负责。

## 检查

```bash
docker run --rm \
  -v "$PWD:/work/example_ranking_source" \
  tinyping/sakuramediabe:v0.15.7 \
  python /app/cli.py plugins check /work/example_ranking_source
```

期望输出：`插件 example_ranking_source 校验通过`。

## 安装试用

把本目录打包为 zip（`manifest.json` 和 `__init__.py` 位于根目录），上传安装、启用并重启容器。
在「任务」里手动运行「同步示例排行榜」，或在「发现 → 排行榜」查看该来源。

完整契约见 [插件开发文档](https://tinypinglite.github.io/sakuramedia/zh-CN/guide/plugin-development)。
