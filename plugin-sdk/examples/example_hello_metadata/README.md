# 示例：静态元数据源

演示注册 `catalog.metadata_source` 扩展点，按番号返回静态影片元数据。

- 只对 `EXAMPLE-001` 返回结果，其他番号返回 `None`，宿主会继续尝试下一个来源。
- 封面在 `fetch_movie()` 调用时复制到 `context.data_dir / "metadata-tmp"`，符合宿主对图片交付路径的约定。
- 配置项 `title_prefix` 演示 Pydantic 设置模型和 `title_i18n` / `description_i18n`。
- `i18n/` 演示静态文案和运行时 `context.i18n.t()`。

## 检查

在宿主容器里校验（不需要数据库；挂载目录名必须等于 `plugin_id`）：

```bash
docker run --rm \
  -v "$PWD:/work/example_hello_metadata" \
  tinyping/sakuramediabe:v0.15.7 \
  python /app/cli.py plugins check /work/example_hello_metadata
```

期望输出：`插件 example_hello_metadata 校验通过`。

## 安装试用

把本目录打包为 zip，注意 `manifest.json` 和 `__init__.py` 必须位于 zip 根目录：

```bash
cd .. && zip -r example_hello_metadata.zip example_hello_metadata -x '*/data/*'
```

在「系统设置 → 插件」上传安装、启用并重启容器。插件设置页里可以修改标题前缀；
用 `EXAMPLE-001` 触发导入或插件能力测试即可看到静态元数据。

完整契约见 [插件开发文档](https://tinypinglite.github.io/sakuramedia/zh-CN/guide/plugin-development)。
