# 外部服务测试命令

本文说明用于手工验证外部服务可用性的 `click` 测试命令。

这些命令适合在开发、联调和排障时直接测试：

- 大模型翻译服务是否可连通
- JavDB 按番号拉详情是否可用
- DMM 按番号抓简介是否可用

约定：

- 命令入口统一是 `src.start.commands`
- 这些命令不会初始化数据库
- 这些命令不会写任务记录
- 默认读取当前 `config.toml` 中的翻译服务、JavDB、DMM 和闭源 Provider 授权配置
- 传入 `--json` 时会输出稳定 JSON，便于脚本集成

## 翻译服务

命令：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-trans --text "これはテストです"
```

从文件读取待翻译文本：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-trans --text-file /tmp/source.txt
```

覆盖默认 prompt、模型和服务地址：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-trans \
  --text "この作品は..." \
  --prompt "请把文本翻译成自然的简体中文，只返回译文。" \
  --base-url http://127.0.0.1:8000 \
  --api-key sk-test \
  --model gpt-4o-mini
```

JSON 输出：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-trans --text "こんにちは" --json
```

说明：

- `--text` 和 `--text-file` 必须二选一
- `--prompt` 和 `--prompt-file` 最多传一个
- 都不传时会使用默认“翻译为简体中文” prompt
- 命令不会检查 `movie_info_translation.enabled`，方便单独调试外部大模型服务

## JavDB

按番号拉取详情：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-javdb --movie-number ABP-123
```

强制走 metadata 代理：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-javdb --movie-number ABP-123 --use-metadata-proxy
```

JSON 输出：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-javdb --movie-number ABP-123 --json
```

说明：

- 默认复用 `build_javdb_provider(use_metadata_proxy=False)`；站点请求由闭源 `sakuramedia-metadata-providers` 提供
- `--use-metadata-proxy` 会让 JavDB 与 GFriends 一起走统一 metadata 代理
- 未激活或授权过期时，命令会返回授权错误，需要先通过 `/metadata-provider-license/activate` 激活
- 命令会输出影片标题、JavDB ID、演员数量、标签数量和简介摘要，方便快速判断接口是否正常

## DMM

按番号抓简介：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-dmm --movie-number ABP-123
```

JSON 输出：

```bash
~/miniconda3/envs/sakuramedia/bin/python -m src.start.commands test-dmm --movie-number ABP-123 --json
```

说明：

- 命令复用 `build_dmm_provider()`；站点请求由闭源 `sakuramedia-metadata-providers` 提供
- 代理取自 `settings.metadata.proxy`；旧版 `metadata.dmm_proxy` 仅在 `proxy` 为空时作为兼容回退
- 未激活或授权过期时，命令会返回授权错误，需要先通过 `/metadata-provider-license/activate` 激活
- 如果 DMM 搜索不到对应番号，或详情页没有简介，会直接返回非零退出码

## 常见报错

- `movie_desc_translation_unavailable`
  - 当前大模型服务不可达、超时或网络被拒绝，优先检查 `movie_info_translation.base_url`、容器网络和 API Key
- `metadata request failed`
  - JavDB 或 DMM 远端请求失败，优先检查闭源 Provider 授权状态、代理配置、目标站点可访问性和当前网络环境
- `--json` 模式退出码非零
  - 命令仍会输出结构化错误对象，可读取 `error.type`、`error_code`、`status_code` 或请求 URL 辅助排查
