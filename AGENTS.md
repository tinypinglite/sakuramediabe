## python路径

`~/miniconda3/envs/sakuramedia/bin/python`


# Repository Guidelines



## 项目结构与分层边界

- 主代码在 `src/`，整体依赖方向保持 `api -> service -> model`。`schema` 负责协议与转换，`metadata` 负责外部站点适配，`scheduler` / `start` 负责任务编排与入口。
- `src/api/routers/` 是 FastAPI 接入层，当前按 `catalog`、`collections`、`discovery`、`files`、`playback`、`system`、`transfers` 七个域组织；统一入口是 `src/api/app.py` 的 `create_app(...)`。涉及启动期行为时，一并关注 lifespan 中的 `initdb`、任务恢复和媒体元数据回填后台线程。
- `src/service/` 是业务编排层，按 `catalog`、`collections`、`discovery`、`playback`、`system`、`transfers` 分域；router 保持薄层，不要在接口层写复杂查询、状态流转或抓取逻辑。
- `src/service/discovery/` 除排行榜和图像搜索外，还包含热评与影片推荐/相似度逻辑；改动相似影片能力时，优先沿用 `MovieRecommendationService`。
- `src/service/system/` 负责账号认证、系统状态、活动流、通知、资源任务状态、索引器设置、合集番号特征等系统级能力；涉及后台任务可观测性时，优先检查 `ActivityService` 和 `ResourceTaskStateService`。
- `src/model/` 是 Peewee 模型层，当前分为 `catalog`、`collections`、`discovery`、`playback`、`system`、`transfers` 六个子域；新增模型时优先关注 `src/model/__init__.py`、对应子域 `__init__.py`、`src/start/initdb.py`、`tests/conftest.py` 里的 `TEST_MODELS`。
- `src/schema/` 是 Pydantic 2 协议层，优先复用 `src/schema/common/base.py` 的 `SchemaModel` 做 Peewee / attributes -> Pydantic 转换。
- `src/metadata/` 维护 JavDB、DMM、MissAV、GFriends 等站点适配；代理、请求错误和 provider 工厂统一沿用现有模式，不要在 service 中散落请求细节。
- `src/joytag_infer/` 是独立的推理服务入口与配置，配合 `src/start/start_joytag_infer.py` 使用；图像搜索嵌入链路改动时，不要只改业务侧 client。
- `src/scheduler/registry.py` 维护所有定时任务注册表，`src/start/aps.py` 负责调度器装配，`src/start/commands.py` 同时暴露 APS 子命令和若干独立 CLI。
- `docs/` 维护 API、部署和约定文档，当前包含 `catalog`、`collections`、`discovery`、`playback`、`system`、`transfers`、`deployment`、`plans`，以及根目录 `README.md`、`conventions.md`；行为变更后要同步更新相关文档。
- `tests/` 按 `api`、`common`、`config`、`metadata`、`model`、`service`、`start` 分层，另有 `tests/javdb_api_samples/` 作为样例数据；数据库相关测试统一复用 `tests/conftest.py`。
- `lib/joytag`、`storage/`、`logs/`、`docker-data/`、根目录 `sakuramedia.db` 都是当前仓库实际存在的本地资源或运行产物；处理问题前先确认是否真的需要改动。

## 常用开发命令

- 安装依赖：`poetry install`
- 启动 API：`poetry run python src/start/startapi.py`
- 启动 JoyTag 推理服务：`poetry run python src/start/start_joytag_infer.py`
- 初始化数据库：`poetry run python -m src.start.commands initdb`
- 启动调度器：`poetry run python -m src.start.commands aps`
- 单次执行 APS 任务：
  - `poetry run python -m src.start.commands aps sync-subscribed-actor-movies`
  - `poetry run python -m src.start.commands aps auto-download-subscribed-movies`
  - `poetry run python -m src.start.commands aps update-movie-heat`
  - `poetry run python -m src.start.commands aps sync-movie-interactions`
  - `poetry run python -m src.start.commands aps sync-rankings`
  - `poetry run python -m src.start.commands aps sync-hot-reviews`
  - `poetry run python -m src.start.commands aps sync-movie-collections`
  - `poetry run python -m src.start.commands aps sync-download-tasks`
  - `poetry run python -m src.start.commands aps auto-import-download-tasks`
  - `poetry run python -m src.start.commands aps scan-media-files`
  - `poetry run python -m src.start.commands aps sync-movie-desc`
  - `poetry run python -m src.start.commands aps translate-movie-desc`
  - `poetry run python -m src.start.commands aps generate-media-thumbnails`
  - `poetry run python -m src.start.commands aps index-image-search-thumbnails`
  - `poetry run python -m src.start.commands aps recompute-movie-similarities`
  - `poetry run python -m src.start.commands aps optimize-image-search-index`
- 独立 CLI：
  - `poetry run python -m src.start.commands add-media-library --name <name> --root-path <abs_path>`
  - `poetry run python -m src.start.commands import-media --source-path <dir> --library-id <id>`
  - `poetry run python -m src.start.commands backfill-media-metadata`
  - `poetry run python -m src.start.commands cleanup-movie-subtitle-fetch-history`
  - `poetry run python -m src.start.commands scan-media-files`
  - `poetry run python -m src.start.commands test-trans --text "これはテストです"`
  - `poetry run python -m src.start.commands test-javdb --movie-number ABP-123`
  - `poetry run python -m src.start.commands test-dmm --movie-number ABP-123`
- 测试：
  - 全量：`poetry run pytest`
  - 单文件：`poetry run pytest tests/service/test_media_import_service.py`
  - 单用例：`poetry run pytest tests/api/test_movie_api.py::test_list_movies_supports_status_subscribed -q`

## 变更联动清单

- 新增 API：同步检查对应 router / service / schema、`src/api/app.py` 的路由注册、鉴权 / `db_deps` 依赖，以及 `docs/` 中对应域文档。
- 新增定时任务：至少同步检查 `src/config/config.py` 的 `Scheduler` 配置、`src/scheduler/registry.py` 的任务注册、`src/start/aps.py` 的调度装配、`src/start/commands.py` 的 APS 子命令暴露，以及 `tests/start/test_aps.py`。
- 新增独立 CLI：同步检查 `src/start/commands.py`、对应 service、`docs/deployment/commands.md` / `docs/deployment/external-service-tests.md` 与 `tests/start/`。
- 新增模型：至少同步检查 `src/model/__init__.py` 导出、对应子域 `__init__.py`、`src/start/initdb.py` 建表列表、`tests/conftest.py` 中的 `TEST_MODELS`。
- 改动下载、导入、缩略图、图像检索、简介回填/翻译、字幕识别等状态流转逻辑时，不要只改 router；优先复用现有 service 边界，并同步检查 `ResourceTaskStateService`、`ActivityService`、`recover_interrupted_tasks(...)` 链路是否仍然一致。
- 图像搜索相关改动优先沿用 `JoyTagEmbedderClient`、`LanceDbThumbnailStore`、`ImageSearchIndexService`，必要时连同 `src/joytag_infer/` 与状态页健康检查一起更新。
- 下载域当前围绕 Jackett、qBittorrent、本地索引器配置、自动导入与导入作业状态展开；新增下载源或状态同步逻辑时，优先延续现有 `download_*_service.py`、`download_*_client.py`、`media_import_service.py` 的拆分方式。
- 改动系统活动流、通知或资源任务状态接口时，至少同步检查 `src/api/routers/system/activity.py`、`src/schema/system/activity.py`、`src/schema/system/resource_task_state.py` 与对应测试。
- 改动影片相似度或推荐逻辑时，优先复用 `MovieRecommendationService` 和 `MovieSimilarity`，并同步检查推荐接口与重算任务。

### 排行榜约定

- 排行榜属于 `discovery` 域；查询入口是 `RankingCatalogService`，同步入口是 `RankingSyncService`。
- 来源注册表维护在 `RANKING_SOURCES`，`GET /ranking-sources`、`GET /ranking-sources/{source_key}/boards` 和 `GET /ranking-sources/{source_key}/boards/{board_key}/items` 读取的是注册表加查询结果，不是数据库配置表。
- 对外暴露的是可读 `board_key`，provider 原始值通过 `provider_raw_key` 保留在 service 内部，不要把 `0/1/3` 直接暴露给 API。
- 同步链路保持“抓榜单番号 -> 拉详情入库 -> 写 `RankingItem`”的模式；影片入库继续复用 `CatalogImportService.upsert_movie_from_javdb_detail(...)`。
- 当前写库策略是按 `source_key + board_key + period` 整榜替换；影片入库失败时跳过条目，不写占位记录。
- 排行榜改动至少补三类测试：API、service、start。

## 代码与测试约定

- 技术栈以 Python 3.10、FastAPI、Peewee、Pydantic 2、APScheduler 为主。
- 使用 4 空格缩进、UTF-8 编码；模块/函数/变量用 `snake_case`，类名用 `PascalCase`，常量用全大写下划线。
- 保持代码直接、可读，不要为了抽象引入多余包装或与现有风格不一致的技巧。
- 复用现有错误模型、分页 schema、响应 schema，不要平行再造 DTO。
- 不要在 router 或 service 中手写大段字段搬运代码；Peewee -> Pydantic 优先使用 `SchemaModel.from_attributes_model()`、`SchemaModel.from_peewee_model()`、`SchemaModel.from_items()`，必要时再用 `model_to_dict(...) + model_validate(...)`。
- 涉及 `src/start/initdb.py` 或其他手写 SQL 的表结构变更时，禁止写死数据库方言专属类型或语法；列类型必须与当前 Peewee 数据库方言一致，至少同时兼容 SQLite 和 PostgreSQL，并补对应的 `tests/start/test_initdb.py` 用例。
- 迁移补列涉及默认值时，默认值 SQL 必须通过 Peewee 上下文与 `Value(...)` / converter 生成，禁止手写 `TRUE/FALSE`、`0/1` 等字面量拼接，避免 SQLite/PostgreSQL 类型不一致。
- 当前系统是单账号架构；涉及账号、权限、刷新令牌时，不要擅自引入多租户或多账号语义。
- 测试框架是 `pytest`；新增或修改 router、service、schema 转换、metadata provider、CLI、scheduler、joytag 推理服务时，都要同步补测试。
- API 测试优先复用 `tests/conftest.py` 里的 `client`、`app`、`account_user` 等 fixture，不要重复创建测试应用。
- 推荐优先参考的现有测试：
  - API：`tests/api/test_router_layout.py`、`tests/api/test_auth_api.py`、`tests/api/test_movie_api.py`、`tests/api/test_media_api.py`、`tests/api/test_activity_api.py`、`tests/api/test_ranking_sources_api.py`、`tests/api/test_collection_number_features_api.py`、`tests/api/test_system_resource_task_states.py`
  - service：`tests/service/test_movie_service_queries.py`、`tests/service/test_actor_service_queries.py`、`tests/service/test_media_import_service.py`、`tests/service/test_image_search_index_service.py`、`tests/service/test_download_service.py`、`tests/service/test_ranking_service.py`、`tests/service/test_recommendation_service.py`、`tests/service/test_resource_task_state_service.py`、`tests/service/test_media_thumbnail_service.py`
  - start：`tests/start/test_aps.py`、`tests/start/test_add_media_library_command.py`、`tests/start/test_import_media_command.py`、`tests/start/test_external_service_test_commands.py`、`tests/start/test_backfill_media_metadata_command.py`、`tests/start/test_initdb.py`

## 配置、安全与项目边界

- 配置优先从 `/data/config/config.toml` 读取，缺失时回退到 `src/config/config.toml`；配置样例请写到仓库根目录的 `config.example.toml` 或 `config.example.full.toml`，不要回写真实运行配置。
- 严禁提交真实凭据、Cookie、下载器密钥、数据库连接信息或未脱敏日志。
- 涉及图片、视频、字幕访问时，注意当前项目走签名 URL 机制；不要绕过 `src/common/file_signatures.py` 的校验链路。
- `logs/`、`storage/`、`docker-data/`、本地数据库与索引目录默认视为运行产物；除非任务明确要求，不要把这些目录当成业务代码修改目标。
- 项目进入快速迭代阶段，需要考虑数据库迁移和旧版本兼容；写文档、补说明、更新 AGENTS 时，以当前代码为准，不要根据历史描述或未来规划臆测实现。

## 代码编写规范

在编写代码时，始终保持注释的习惯，在关键代码或者是核心逻辑处，使用中文添加简短精准的注释。避免采用降级处理、兜底方案、临时补丁、启发式方法、局部稳定化手段，以及非严谨通用算法的后处理补救措施。

