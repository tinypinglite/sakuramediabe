# SakuraMedia 插件开发 SDK

面向第三方插件开发者的示例、manifest Schema 和类型存根。

> **Host API 18**：当前宿主只接受声明 `host_api_version: 18` 的插件。Host API 升级后，旧插件会停止加载，必须同步更新插件版本。

完整的 API 契约、`context` 能力、扩展点、设置、多语言、安装排错和版本规则，以 Wiki 的
[插件开发](https://tinypinglite.github.io/sakuramedia/zh-CN/guide/plugin-development) 为唯一事实源。
本目录只提供可以直接运行和验证的示例、Schema 与 stub，不重复 Wiki 正文。

## 安全提示

插件在 SakuraMedia 后端进程内运行，拥有宿主开放的全部能力：网络、文件系统、`/data` 下的运行数据与配置。**只安装你信任的插件。** 本目录的示例仅用于演示契约，不是生产实现。

## 目录

```text
plugin-sdk/
├── examples/
│   ├── example_hello_metadata/    # 静态元数据源 + 设置 + 图片交付
│   ├── example_ranking_source/    # 静态排行榜 + 定时任务
│   └── example_reviews_stats/     # 评论 + 互动数
├── schema/
│   └── manifest.schema.json       # manifest.json 的 JSON Schema
├── stubs/
│   └── src/...                    # src.plugins / src.scheduler.contracts 的类型存根
└── tools/
    └── export_from_host.py        # 维护者工具：从宿主源码重新生成 schema 和 stubs
```

每个示例都是完整的插件包：根目录包含 `manifest.json` 和 `__init__.py`，可以直接打包成 zip 安装。

## 检查示例

不需要宿主源码，也不需要数据库。把示例目录挂到容器里，目录名必须等于 `plugin_id`：

```bash
docker run --rm \
  -v "$PWD/examples/example_hello_metadata:/work/example_hello_metadata" \
  tinyping/sakuramediabe:v0.15.7 \
  python /app/cli.py plugins check /work/example_hello_metadata
```

期望输出：`插件 example_hello_metadata 校验通过`。

三个示例可以一次跑完：

```bash
./verify_examples.sh
```

说明：

- 镜像 tag 必须与 Host API 18 兼容；`v0.15.7` 是本文写作时的正式版，升级宿主后换成对应 tag。
- 挂载目录必须是可写的：`plugins check` 会在插件目录下创建 `data/`。`verify_examples.sh` 会先把示例复制到临时目录，不会在仓库里留下 `data/`。
- `plugins check` 只验证 import、`register()`、契约和语言包结构，不代表联网行为、图片交付或任务执行已经通过验收。

## 示例说明

| 示例 | 演示内容 |
|---|---|
| `example_hello_metadata` | `catalog.metadata_source`、Pydantic 设置模型、`title_i18n`、封面交付到 `data/metadata-tmp/`、`context.i18n.t()` |
| `example_ranking_source` | `discovery.ranking_source`、`PluginRankingBoard`、定时任务、`context.sync_ranking_sources()` |
| `example_reviews_stats` | `catalog.movie_reviews` 与 `catalog.movie_stats`、`heat`、`heat_version`、互动数字段声明 |

## manifest Schema

`schema/manifest.schema.json` 由宿主当前的 `PluginManifest` 生成，编辑器可以用它提供补全和校验。

注意：**不要在 `manifest.json` 里写 `$schema` 字段**。宿主对 manifest 使用 `extra="forbid"`，未知字段会导致加载失败。请在编辑器里把 `manifest.json` 映射到这个 Schema，例如 VS Code：

```json
{
  "json.schemas": [
    {
      "fileMatch": ["plugin-sdk/examples/*/manifest.json", "**/plugins/*/manifest.json"],
      "url": "./plugin-sdk/schema/manifest.schema.json"
    }
  ]
}
```

## 类型存根（stubs）

`stubs/` 是 `src.plugins`、`src.plugins.provider_protocol` 和 `src.scheduler.contracts` 的 `.pyi`
类型存根。宿主源码不公开、镜像内是编译产物，stub 是本地获得补全和类型检查的方式。

pyright：

```json
{
  "stubPath": "plugin-sdk/stubs"
}
```

mypy：

```bash
MYPYPATH=plugin-sdk/stubs mypy your_plugin/
```

类型检查环境至少需要安装 `pydantic>=2`；使用媒体 Provider 契约时还需要 `starlette`。

限制：

- stub 只用于开发，**不要打进插件 zip**，也不要在运行时依赖它；运行时始终使用宿主自带的 `src.plugins`。
- Pydantic 模型的字段在 stub 中统一标为可选，必填约束由宿主在运行时校验。
- stub 是 Host API 18 的快照。Host API 变更后必须重新生成。

## 版本兼容

- `manifest.json` 的 `host_api_version` 必须等于 `18`。
- `register()` 返回的 `host_api_version` 必须等于 `18`（通常直接使用 `HOST_API_VERSION` 常量）。
- `manifest.json` 与 `register()` 的插件 `version` 必须完全一致。
- 宿主升级 Host API 后，旧插件不再加载；插件作者需要按新契约重新发布。

## 发布到插件市场

插件包根目录必须包含 `manifest.json` 和 `__init__.py`，zip 不能再套一层目录。社区插件通过
GitHub Release 分发，市场索引每天从最新 Release 同步版本、下载地址和 sha256。投稿规则见
[插件市场 README](https://github.com/tinypinglite/sakuramedia-plugin-market)。

## 维护生成物

`schema/manifest.schema.json` 和 `stubs/` 都是生成物，不要手改。在宿主源码仓库里运行：

```bash
cd /path/to/SakuraMediaBE-source
uv run --with mypy python /path/to/plugin-sdk/tools/export_from_host.py --host-root .
```

修改 `src/plugins` 公开契约或提升 Host API 后，必须重新生成并在同一个改动里更新 Wiki 文档和示例。

## License

示例和文档随本仓库 README 的许可声明，仅允许非商业用途。
