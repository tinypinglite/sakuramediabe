# 部署与调试命令

本文整理容器启动后常用的初始化、单次任务与外部服务探测命令。

## 基础命令

### 构建 Docker 镜像

普通运行时镜像保留源码，适合本地调试和快速验证：

```bash
docker build -t sakuramediabe:plain .
```

发布用混淆镜像只复制 PyArmor 产物，不会把原始 `src` 源码复制到最终镜像层：

```bash
docker build --target obfuscated -t sakuramediabe:obfuscated .
```

说明：

- 混淆镜像使用 PyArmor-only 方案，不引入授权 secret、Cython 或 Nuitka。
- 混淆目标保留与普通镜像一致的运行入口，容器启动时仍会先执行 `migrate`，再执行 `initdb`。
- 容器启动依赖挂载 `/data/config/config.toml`，发布镜像不会内置本地配置文件。
- GitHub Release 触发的 Docker Hub 正式发布也会构建 `obfuscated` target。
- 该方案用于提高发布镜像源码逆向成本，不等同于不可破解的代码保护。

JoyTag 推理服务使用独立源码目录 `docker/joytag-infer/app`，对应镜像不会复制主项目 `src`：

```bash
docker build -f docker/joytag-infer/Dockerfile.cpu -t joytag-infer:cpu .
docker build -f docker/joytag-infer/Dockerfile.openvino -t joytag-infer:openvino .
docker build -f docker/joytag-infer/Dockerfile.cuda -t joytag-infer:cuda .
```

- 初始化数据库：

```bash
poetry run python -m src.start.commands initdb
```

说明：

- `initdb` 只会按当前 Peewee 模型建表，不再兼容旧数据库补列、补索引或旧字段回填。
- `initdb` 只负责建表和初始化默认数据，不会执行待应用 migration，也不是旧库升级入口。

- 执行待应用的数据库迁移：

```bash
poetry run python -m src.start.commands migrate
```

说明：

- `migrate` 只执行尚未落库的 Peewee migration 脚本。
- 命令会先按当前 Peewee 模型补齐缺失表，再执行待应用 migration，但不会运行默认账号/系统播放列表初始化。
- 当前仓库内置 migration 只覆盖仍在支持范围内的历史 schema 差异；现阶段 `movie` 表补列场景只处理 `title_zh`。
- 容器内默认会在启动 supervisor 之前先执行一次 `migrate`，再幂等执行一次 `initdb` 以补齐默认账号和系统播放列表；容器外手动启动 API 或 APS 前，需要先显式执行 `migrate`，新库若需要默认账号还要额外执行 `initdb`。

- 本地启动 API：

```bash
poetry run uvicorn src.api.app:app --workers 1 --host 0.0.0.0 --port 8000
```

说明：

- API lifespan 只负责连接数据库和回收中断任务，不会自动建表或初始化默认数据。
- 本地新库第一次直接启动 API 前，必须先执行 `migrate`；如果还没有默认账号和系统播放列表，再执行 `initdb`。
- 未初始化的新库直接启动 API 时，任务恢复逻辑会读取 `background_task_run`，缺表会导致 `relation "background_task_run" does not exist`。
- Docker 容器入口已经在启动 supervisor 前执行 `migrate` 和 `initdb`，因此生产容器通常不会遇到这个问题。

- 启动 APS 调度器：

```bash
poetry run python -m src.start.commands aps
```

说明：

- `aps` 不再在启动期自动执行 migration；请先通过容器入口脚本或手动执行 `migrate` 完成 schema 升级。

- 单次执行 APS 任务：

```bash
poetry run python -m src.start.commands aps sync-subscribed-actor-movies
poetry run python -m src.start.commands aps update-movie-heat
poetry run python -m src.start.commands aps sync-rankings
poetry run python -m src.start.commands aps sync-hot-reviews
poetry run python -m src.start.commands aps sync-movie-collections
poetry run python -m src.start.commands aps translate-movie-title
poetry run python -m src.start.commands aps generate-media-thumbnails
poetry run python -m src.start.commands aps index-image-search-thumbnails
poetry run python -m src.start.commands aps optimize-image-search-index
```

- 清理已废弃的影片字幕抓取历史任务记录：

```bash
poetry run python -m src.start.commands cleanup-movie-subtitle-fetch-history
```

- 媒体库相关：

```bash
poetry run python -m src.start.commands add-media-library --name Main --root-path /data/media
poetry run python -m src.start.commands import-media --source-path /data/import --library-id 1
poetry run python -m src.start.commands backfill-movie-thin-cover-images
```

- 历史竖封面图回填：

```bash
poetry run python -m src.start.commands backfill-movie-thin-cover-images
```

说明：

- 该命令只处理当前 `thin_cover_image` 为空的影片，不会覆盖已有竖封面图。
- 每部影片都会优先尝试基于封面裁切竖封面，裁切失败后再回退到前两张剧情图中的第一张竖图。
- 命令输出 `scanned_movies`、`updated_movies`、`skipped_movies`、`failed_movies` 四个统计字段，便于一次性补算历史数据。

## 外部服务测试命令

以下命令用于手工验证外部接口是否可用，以及返回结果是否符合预期。

- 这些命令不会调用数据库初始化，也不会写任务记录。
- 默认读取当前 `config.toml` 中的翻译服务、JavDB 和 DMM 配置。
- 传入 `--json` 时会输出稳定 JSON，适合脚本集成或自动化检查。

### 测试翻译服务

直接传文本：

```bash
poetry run python -m src.start.commands test-trans --text "これはテストです"
```

从文件读取待翻译文本：

```bash
poetry run python -m src.start.commands test-trans --text-file /tmp/source.txt
```

覆盖默认 prompt、模型与服务地址：

```bash
poetry run python -m src.start.commands test-trans \
  --text "この作品は..." \
  --prompt "请把文本翻译成自然的简体中文，只返回译文。" \
  --base-url http://127.0.0.1:8000 \
  --api-key sk-test \
  --model gpt-4o-mini
```

JSON 输出：

```bash
poetry run python -m src.start.commands test-trans --text "こんにちは" --json
```

说明：

- `--text` 和 `--text-file` 必须二选一。
- `--prompt` 和 `--prompt-file` 最多传一个；都不传时会使用默认“翻译为简体中文” prompt。
- 命令不会检查 `movie_info_translation.enabled`，方便单独调试外部大模型服务。

### 测试 JavDB

按番号拉取详情：

```bash
poetry run python -m src.start.commands test-javdb --movie-number ABP-123
```

强制走 metadata 代理：

```bash
poetry run python -m src.start.commands test-javdb --movie-number ABP-123 --use-metadata-proxy
```

JSON 输出：

```bash
poetry run python -m src.start.commands test-javdb --movie-number ABP-123 --json
```

说明：

- 默认复用 `build_javdb_provider(use_metadata_proxy=False)`。
- `--use-metadata-proxy` 会让 JavDB 与 GFriends 一起走统一 metadata 代理。
- 命令会输出影片标题、JavDB ID、演员数量、标签数量和简介摘要，便于快速判断接口是否正常。

### 测试 DMM

按番号抓简介：

```bash
poetry run python -m src.start.commands test-dmm --movie-number ABP-123
```

JSON 输出：

```bash
poetry run python -m src.start.commands test-dmm --movie-number ABP-123 --json
```

说明：

- 命令复用 `build_dmm_provider()`，代理取自 `settings.metadata.proxy`；旧版 `metadata.dmm_proxy` 仅在 `proxy` 为空时作为兼容回退。
- 如果 DMM 搜索不到对应番号，或详情页没有简介，会直接返回非零退出码。

## 常见问题

- 翻译命令返回 `movie_desc_translation_unavailable`
  - 说明当前大模型服务不可达、超时或网络被拒绝。优先检查 `movie_info_translation.base_url`、容器网络和 API Key。
- JavDB 或 DMM 返回 `metadata request failed`
  - 说明远端请求失败。优先检查代理配置、目标站点可访问性以及当前网络环境。
- `--json` 模式退出码非零
  - 命令仍会输出结构化错误对象，可直接读取 `error.type`、`error_code`、`status_code` 或请求 URL 辅助排查。
