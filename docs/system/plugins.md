# SakuraMedia 插件系统开发指南

> 本文面向插件开发者，描述当前版本的稳定契约
> （插件通过 `host_api_version` 与宿主协商兼容版本，当前为 2）。

## 1. 插件是什么

插件就是**插件根目录下的一个子目录**：目录名是 `plugin_id`，目录内必须有
`manifest.json` 与 `__init__.py`。把目录放进 `plugins.root_dir`（默认
`/data/plugins`）、在 `plugins.enabled` 里写上 `plugin_id`、重启 api 与 aps，
宿主就会在 import 阶段加载它。插件可以以目录形式直接拷贝/挂载，也可以
打包成 zip 通过 `plugins install` CLI 或 `/system/plugins` API 安装。

插件通过包根目录的 `register(context)` 向宿主声明自己，注册对象有两个通道：
**后台任务**（`jobs`，宿主平台能力）和
**扩展点声明**（`extensions`，业务领域扩展）。当前扩展点目录只登记了
排行榜来源（`discovery.ranking_source`）；机制本身是通用的——核心只做
结构校验，不解释任何领域的载荷，新增领域不需要改核心契约与加载器。

加载流程：

1. import 插件包根并调用 `register(context)`；
2. 校验 `PluginRegistration`、任务声明与扩展点声明；
3. 把任务合并进 `JOB_REGISTRY`，进入任务中心统一调度。

单个插件加载失败不会拖垮服务：错误记入 `PLUGIN_LOAD_ERRORS`，
`plugins list` 与管理 API 可以看到原因，部署者可以选择停用或删除该插件。

## 2. 能做什么 / 不能做什么

### 2.1 可以开发的插件类型

| 插件类型 | 说明 | 典型场景 |
|---|---|---|
| 定时任务插件 | `default_cron` + `service_factory`，按 cron 入队执行 | 定时抓取外部站点/榜单的番号列表并批量导入影片元数据；定时为主库影片抓字幕；定时拉取外部数据缓存到插件 `data_dir` |
| 手动任务插件 | `manual_only` + `params_schema` + `params_handler`，无 cron、只能手动触发（通常配合参数模型） | 手动指定番号补抓单部影片元数据或字幕；按插件自定义参数处理 `data_dir` 里的任务清单 |
| 混合任务插件 | 同一任务同时声明 cron 与参数模型 | 定时全量批量抓取，手动指定单个番号重试/补抓 |
| 排行榜来源插件 | 声明 `discovery.ranking_source` 扩展点 + 注册同步任务，宿主负责注册、编排与写库（见 6.6） | 提供 JavDB 等站点的排行榜来源；未安装时 `/ranking-sources` 返回空列表 |

注意：表格里的"抓榜单/抓字幕"都是**插件自己访问外部站点抓取**，宿主只提供
写侧能力（影片元数据入库、字幕落盘登记、排行榜来源注册与同步编排），
并不提供抓取链路。

### 2.2 宿主提供的稳定能力

- **影片元数据入库**：`import_movie_by_number()` 通过 JavDB 获取详情并完整落库
  （Movie/Actor/Tag/Image，含图片下载；**纯新建语义——番号或 javdb_id 已存在则跳过、
  不更新任何字段**）；批量任务应复用
  `build_javdb_provider()` + `build_catalog_import_service()`，并用
  `list_existing_movie_numbers()` 做主库存在性判断；已存在影片的元数据刷新唯一入口
  是手动刷新接口（全量覆盖）；
- **字幕资产写入**：`import_subtitle()` 接受插件准备好的字幕字节内容，宿主统一做
  扩展名白名单校验（`.srt/.ass/.ssa/.vtt`）、同影片内容指纹去重、原子落盘与登记；
  **抓取/下载由插件自己负责**，宿主不提供抓字幕的链路；
- **排行榜来源注册与同步编排**：插件通过 `discovery.ranking_source` 扩展点
  声明 `source_key` / boards / 抓取回调，宿主负责注册、同步编排、JavDB 详情入库、
  整榜写 `RankingItem` 与对外 API（见 6.6）；
- **后台任务全链路**：任务进统一任务中心，天然获得任务级互斥、SSE 进度、
  运行记录、通知、崩溃恢复与 `business_recovery` 钩子；
- **网络与数据处理**：插件可以使用宿主 venv 已安装的依赖（httpx 等）与标准库
  访问外部站点；
- **持久化运行数据**：每个插件拥有独立的 `data/` 目录，重新安装不覆盖；
- **只读私有配置**：插件可以读取部署者在 `plugins.settings.<plugin_id>` 下配置的内容。

### 2.3 当前不支持的能力

- 新增 HTTP 路由 / API 端点；
- 注册事件钩子、中间件或 Webhook；
- 扩展 JavDB/GFriends 等 metadata provider 与索引器；
- 按主库资源 ID 做通用处理（插件不能按条件批量查询/更新主库任意资源；
  写侧入口只有 `import_movie_by_number` / `import_subtitle` / `movies.patch` 三个，
  `movies.get` / `find_by_numbers` 只能按 id / 番号单点读取快照）；
- 查询主库业务状态（例如"哪些影片缺字幕/缺封面"），`list_existing_movie_numbers()`
  只能拿到全部番号集合；
- 直接访问数据库；
- 安装第三方依赖（插件只能使用宿主 venv 已装依赖 + 标准库）；
- 注册前端页面或 UI 组件。

这些边界是**设计约束**：插件只应通过 `PluginContext` 与公开类型触碰宿主能力。
插件与宿主同进程运行，代码可以 import 任何模块；但请**只依赖公开契约**，
不要绑定宿主内部实现，否则宿主重构会破坏你的插件。

## 3. 快速开始：最小插件

### 3.1 目录结构

```text
<plugins_root>/subtitle_fetch/
├── manifest.json
├── __init__.py          # 必须暴露 register(context)
└── plugin.py            # 插件实现，可按需自由组织
```

### 3.2 manifest.json

```json
{
  "plugin_id": "subtitle_fetch",
  "display_name": "字幕抓取",
  "version": "1.0.0",
  "host_api_version": 2,
  "requires_python": ">=3.10",
  "author": "example",
  "homepage": "https://example.com/subtitle_fetch"
}
```

### 3.3 插件代码

`__init__.py`：

```python
from .plugin import register

__all__ = ["register"]
```

`plugin.py` 的最小实现：

```python
from src.plugins import PluginContext, PluginRegistration


def register(context: PluginContext) -> PluginRegistration:
    return PluginRegistration(
        plugin_id="subtitle_fetch",
        display_name="字幕抓取",
        version="1.0.0",
    )
```

### 3.4 安装与加载

```bash
# 方式一：直接拷贝/挂载目录
cp -r subtitle_fetch /data/plugins/

# 方式二：CLI 安装目录或 zip（zip 支持 --sha256 完整性校验）
cd subtitle_fetch && zip -r ../subtitle_fetch.zip .
uv run python -m src.start.commands plugins install ../subtitle_fetch.zip

# 启用（写入 plugins.enabled）
uv run python -m src.start.commands plugins enable subtitle_fetch

# 重启 api 与 aps 后生效
```

插件作者可以用 `plugins check <目录>` 在部署前校验：

```bash
uv run python -m src.start.commands plugins check ./subtitle_fetch
```

也可以通过 HTTP API 上传 zip 安装（需要登录鉴权）：

```bash
curl -X POST http://host/api/system/plugins \
  -H "Authorization: Bearer <token>" \
  -F "file=@subtitle_fetch.zip" \
  -F "sha256=<zip-sha256>" \
  -F "enable=true"
```

插件接管 Movie 字段后（v2-lite 字段主权），插件被删除时其接管记录会保留在
movie.field_owners 中，字段冻结回宿主管理前需用清理命令解除接管：

```bash
# 解除某插件对全部 Movie 字段的接管
uv run python -m src.start.commands plugins clear-field-owners --plugin-id subtitle_fetch

# 只解除指定字段（可重复）
uv run python -m src.start.commands plugins clear-field-owners \
  --plugin-id subtitle_fetch --field title --field summary
```

## 4. 插件目录规范

### 4.1 目录要求

- 目录名必须等于 `manifest.plugin_id`，且满足 `^[a-z][a-z0-9_]*$`；
- 目录内必须有 `manifest.json` 与 `__init__.py`；
- 其余 `.py` / 资源文件由插件作者自由组织，支持包内相对导入；
- `data/` 是宿主托管的运行数据目录，插件不应把运行状态写进代码目录；
- 插件目录可以是符号链接或 bind mount，宿主按目录名加载。

### 4.2 manifest.json 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `plugin_id` | 是 | `^[a-z][a-z0-9_]*$`，与目录名、`plugins.enabled` 项一致 |
| `display_name` | 是 | 展示名 |
| `version` | 是 | 插件版本（PEP 440） |
| `host_api_version` | 是 | 插件声明的宿主接口版本，必须满足 `MIN_SUPPORTED <= v <= HOST_API_VERSION`（当前 `MIN_SUPPORTED=1`、`HOST_API_VERSION=2`；以 manifest 声明为准） |
| `requires_python` | 否 | 与宿主 Python 解释器（3.10）校验 |
| `author` / `homepage` | 否 | 展示信息 |

未知字段会被严格拒绝（`extra="forbid"`），不要随意添加自定义字段。

### 4.3 zip 分发限制

- zip ≤ 100MB，解压后 ≤ 500MB，文件数 ≤ 5000；
- 拒绝绝对路径、`..` 越界路径与符号链接成员；
- 可选传入 zip sha256，宿主会校验完整性；`requires_python` 不满足也会拒绝；
- 发布前宿主会**试加载**（真实 import + register + 契约校验），
  坏插件在安装期就被拒绝，不会留到下次启动才报错；
- zip 根必须是插件目录内容（`manifest.json` 与 `__init__.py` 在 zip 根），
  没有文件哈希清单或依赖声明。

## 5. 插件契约

### 5.1 register 与 PluginRegistration

```python
from src.plugins import PluginContext, PluginRegistration

def register(context: PluginContext) -> PluginRegistration:
    ...
```

`PluginRegistration` 字段：

| 字段 | 说明 |
|---|---|
| `plugin_id` | 必须与 manifest / 目录名一致 |
| `display_name` | 展示名 |
| `version` | 必须与 manifest 完全一致 |
| `host_api_version` | 区间校验（当前 `[1, 2]`，以 manifest 声明为准；与 register 声明不一致时告警但以 manifest 为准，见 4.2） |
| `jobs` | `JobDefinition` 元组，允许为空 |
| `extensions` | `PluginExtension` 元组，允许为空；业务领域扩展声明，当前登记的扩展点见 6.6 |

`register(context)` 在 api/aps 启动 import 阶段执行，**只应该做声明，不要做耗时操作**
（网络请求、重型初始化请放进任务执行体）。

### 5.2 JobDefinition

```python
from src.scheduler.contracts import JobDefinition
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `task_key` | 是 | 任务稳定标识；全局唯一 |
| `log_name` | 是 | 任务日志文件名；全局唯一 |
| `cli_name` | 是 | `aps` 子命令名；全局唯一 |
| `cli_help` | 是 | CLI 帮助与任务中心展示文案 |
| `default_cron` | 视形态 | 定时任务的默认 cron 表达式 |
| `service_factory` | 视形态 | `Callable[[TaskRunReporter], Any]`，cron 执行体 |
| `params_schema` | 否 | pydantic `BaseModel` 子类，手动触发时校验请求体 |
| `params_handler` | 否 | `Callable[[TaskRunReporter, dict], Any]`，带参执行体 |
| `manual_only` | 否 | `True` 表示无 cron，只能手动触发（可配合参数模型） |
| `manual_trigger_allowed` | 否 | 是否允许 HTTP 手动触发，默认 `True` |
| `business_recovery` | 否 | 崩溃恢复时联动清理插件业务状态的钩子 |
| `format_stats` | 否 | 把结果 dict 格式化为 CLI 统计文案 |
| `cron_setting` | - | **插件禁止声明**（宿主 Scheduler 字段，由 loader 拒绝） |
| `plugin_id` | - | **禁止自行声明**（loader 注入） |

校验规则（违反则加载失败并隔离该插件）：

- 必须提供 `service_factory` 或 `params_handler` 至少一个；
- cron 任务必须提供 `service_factory`；
- `manual_only` 任务不能声明任何 cron，且必须允许手动触发；
- `params_schema` 与 `params_handler` 必须成对出现；
- `default_cron` 必须是合法 crontab 表达式；
- 同插件内 `task_key` 不能重复。

### 5.3 三种任务形态

**定时任务**

```python
JobDefinition(
    task_key="daily_sync",
    log_name="daily-sync",
    cli_name="sync-daily",
    cli_help="每日同步",
    default_cron="0 4 * * *",
    service_factory=lambda reporter: run_sync(reporter),
)
```

**手动带参任务**

```python
class Params(BaseModel):
    movie_number: str

JobDefinition(
    task_key="fetch_one",
    log_name="fetch-one",
    cli_name="fetch-one",
    cli_help="按番号抓取",
    manual_only=True,
    params_schema=Params,
    params_handler=lambda reporter, params: fetch_one(reporter, params),
)
```

**混合任务**：同时声明 `default_cron + service_factory` 与
`params_schema + params_handler`。定时触发走 `service_factory`（无参），
手动触发带 body 时走 `params_handler`。

### 5.4 任务唯一性

`task_key` / `log_name` / `cli_name` 必须同时满足：

- 不与宿主内建任务重复；
- 不与其他插件任务重复；
- 不与任务队列专属 key 重复。

冲突时**只隔离该插件**（记入 `PLUGIN_LOAD_ERRORS`），内建任务与其它插件不受影响。

### 5.5 手动触发与参数

- `GET /system/jobs` 返回任务元数据，`params_schema` 以 JSON Schema 形式暴露；
- `POST /system/jobs/{task_key}/run` 带 JSON body 触发带参任务；
- CLI 自动为每个任务注册 `aps <cli_name>` 子命令，带参任务额外支持
  `--params-json '{"movie_number": "ABP-123"}'`；
- `manual_only` 且无 `params_schema` 的任务只能走 HTTP 触发（CLI 无法表达参数）。

### 5.6 扩展点声明（PluginExtension）

插件通过 `extensions` 声明业务领域扩展。机制层只做通用校验，不解释
`data` 的领域语义：

| 字段 | 说明 |
|---|---|
| `key` | 点分命名空间（如 `discovery.ranking_source`）；必须被宿主扩展点目录登记 |
| `data` | 领域载荷（可以是带回调的模型实例），由对应领域的校验器解释 |

```python
from src.plugins import PluginExtension

PluginExtension(
    key="discovery.ranking_source",
    data=PluginRankingSource(...),  # 领域载荷
)
```

通用校验规则（违反则加载失败并隔离该插件）：

- `key` 必须匹配 `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$` 且长度 ≤ 128；
- 同插件内 `key` 不能重复；
- `data` 不能为空；
- 宿主未登记的 key 直接拒绝，避免"插件装了但扩展点静默没生效"；
- 领域载荷的具体约束由对应领域的校验器执行（如排行榜的 `source_key` /
  board / 周期规则），失败同样记入 `PLUGIN_LOAD_ERRORS`。

## 6. PluginContext 与公开类型

### 6.1 context 字段

| 字段 | 说明 |
|---|---|
| `plugin_id` | 当前插件 ID |
| `settings` | 插件私有配置（`plugins.settings.<plugin_id>` 的内容），**深冻结只读** |
| `data_dir` | 插件专属数据目录 `<root>/<plugin_id>/data/`，自动创建、重装保留 |

### 6.2 context 方法

| 方法 | 说明 |
|---|---|
| `ensure_data_dir() -> Path` | 确保数据目录存在并返回路径（`data_dir` 属性等价） |
| `build_javdb_provider(username=None, password=None)` | 构造 JavDB 元数据 provider；账号仅需登录的榜单（TOP250）需要，由插件从自身设置传入 |
| `build_catalog_import_service()` | 构造目录入库服务 |
| `import_movie_by_number(movie_number, *, force_subscribed=False)` | 通过 JavDB 获取详情并复用宿主能力入库；**已存在影片跳过不更新**（纯新建语义），返回不可变 `MovieSnapshot`（v2 契约，不再返回可写 ORM 对象） |
| `movies` | 影片只读快照与受保护字段写入出口（v2 契约），见 6.8 节 |
| `list_existing_movie_numbers() -> set[str]` | 主库全部影片番号（大写），用于 O(1) 存在性判断 |
| `import_subtitle(movie_number, content, filename, language=None)` | 写入一段字幕字节内容，返回 `SubtitleImportResult`。只支持 `.srt/.ass/.ssa/.vtt`；去重粒度为**同一部影片内**的内容 sha256；`filename` 只取扩展名；影片不存在返回 `movie_not_found`，不抛异常 |
| `sync_ranking_sources(progress_callback=None)` | 同步当前插件声明的全部排行榜来源，返回统计 dict |
| `sync_ranking_board(source_key, board_key, period=None)` | 同步单个榜单；`source_key` 必须是本插件声明的来源 |
| `get_task_logger(name)` | 获取绑定到任务日志文件的 loguru logger |

性能建议：`import_movie_by_number` 每次会新建 provider/importer，
**批量任务**应使用 `build_javdb_provider()` + `build_catalog_import_service(...)`
在任务内复用，避免每个番号重复构造客户端。

语义边界：`import_subtitle` 只做写侧，影片必须已存在于主库
（否则返回 `movie_not_found`）；批量抓字幕前可以用 `list_existing_movie_numbers()`
过滤，或先调用 `import_movie_by_number()` 把影片建出来。

### 6.3 公开类型

插件从 `src.plugins` / `src.plugins.types` 导入公开类型：

```python
from src.plugins.types import (
    ImageDownloadError,
    JavdbMovieActor,
    JavdbMovieDetail,
    JavdbMovieTag,
    SubtitleImportResult,
    SubtitleImportStatus,
)
```

- `JavdbMovieDetail / JavdbMovieActor / JavdbMovieTag`：JavDB 元数据模型；
- `SubtitleImportResult / SubtitleImportStatus`：字幕写入结果
  （`imported / duplicate / movie_not_found / invalid_format`）；
- `MovieSnapshot / MOVIE_SNAPSHOT_FIELDS`：影片不可变快照（v2 契约），见 6.8 节；
- `ImageDownloadError`：入库时图片下载失败异常。

### 6.4 任务 reporter

任务执行体接收 `TaskRunReporter`，用于进度与统计上报：

```python
def handler(reporter, params):
    reporter.emit(
        current=1,
        total=10,
        text="processing ...",
        summary_patch={"processed": 1},
    )
    return {"done": True}  # dict 会自动合并进任务结果统计
```

进度会写入任务运行记录，通过 `GET /system/task-runs` 与 SSE 事件流
（`/system/events/stream`）对外可见；任务完成/失败会走通知中心。

### 6.5 日志

- 每个任务按 `log_name` 自动写独立日志文件（`<scheduler.log_dir>/<log_name>.log`）；
- 插件代码里可用 `context.get_task_logger(name)` 获取绑定的 loguru logger。

### 6.6 排行榜来源扩展点（discovery.ranking_source）

排行榜**不是默认功能**：宿主不内置任何来源，来源全部由排行榜插件提供。
插件在 `register(context)` 里通过 `extensions` 声明来源。这是当前唯一登记的扩展点：

```python
from src.plugins import (
    PluginContext,
    PluginExtension,
    PluginRankingBoard,
    PluginRankingSource,
    PluginRegistration,
    RANKING_SOURCE_EXTENSION_KEY,
)

def _fetch_hot(period: str) -> list[str]:
    # 插件自己抓取外部站点，返回番号列表（顺序即 rank）
    return ["ABP-123", "IPX-456", ...]

def register(context: PluginContext) -> PluginRegistration:
    return PluginRegistration(
        plugin_id="xxx_rank",
        display_name="XXX 榜",
        version="1.0.0",
        extensions=(
            PluginExtension(
                key=RANKING_SOURCE_EXTENSION_KEY,
                data=PluginRankingSource(
                    source_key="xxx",
                    name="XXX 站",
                    boards=(
                        PluginRankingBoard(
                            key="hot",
                            name="热榜",
                            supported_periods=("daily", "weekly", "monthly"),
                            default_period="daily",
                            fetch_numbers=_fetch_hot,
                        ),
                    ),
                ),
            ),
        ),
    )
```

字段语义：

| 字段 | 说明 |
|---|---|
| `source_key` | 全局唯一稳定标识；不加前缀、不设保留字；跨插件冲突时后加载插件整插件隔离 |
| `board.key` | 来源内唯一 |
| `supported_periods` | 静态周期；空表示单期榜（API 不接受 period） |
| `supported_periods_provider` | 动态周期回调（如 top250 年份逐年滚动）；与静态周期二选一，要求纯本地、幂等、无副作用 |
| `default_period` | 可选，必须属于最终周期集合 |
| `should_fetch(period, has_items)` | 抓取前回调，返回 `False` 则跳过（如历史年份已抓过） |
| `fetch_numbers(period)` | 返回番号列表，顺序即 rank；异常冒泡给宿主统一处理 |

需登录榜单的账号由插件自己管理：从 `plugins.settings.<plugin_id>` 读取账号、
通过 `context.build_javdb_provider(username=..., password=...)` 构建 provider，
并在 `should_fetch` 里表达「未配置账号就不抓」。宿主不感知账号状态，
抓取失败的异常统一计入 `failed_targets`。

同步节奏由插件自己表达：插件注册 cron/手动任务，执行体调用
`context.sync_ranking_sources()`（全部来源）或
`context.sync_ranking_board(source_key, board_key, period)`（单个榜单）。
宿主的 `ranking_sync` 内置任务已移除，未安装排行榜插件时
`GET /ranking-sources` 返回空列表。

### 6.7 扩展点不是必须的：机制边界

扩展点只给"宿主有消费方"的领域用。判据是：**宿主是否需要注册、枚举、编排、
合并插件的声明**。需要才登记扩展点；不需要就只用 `jobs` + `context`。

- **字幕插件、全量影片抓取插件不需要扩展点**：插件自己驱动任务循环，
  通过 `context.import_subtitle()` / `import_movie_by_number()` 单次写入宿主，
  宿主不需要知道"这个字幕是谁提供的""抓取源有几个"；
- **排行榜需要扩展点**：宿主拥有 `RankingItem` 数据、`/ranking-sources` API
  和同步管线，必须把插件的 `source_key` / boards 收编进自己的注册表，
  跨插件冲突由宿主裁决。

### 6.8 影片快照与受保护字段（v2 契约）

`HOST_API_VERSION` 为 2（`MIN_SUPPORTED_HOST_API_VERSION` 保持 1，v1 插件可以继续
加载，但运行期行为按 v2 语义：`import_movie_by_number` 返回的是不可变
`MovieSnapshot` 而非可写 ORM 对象，仍依赖旧返回值属性的插件必须升级）。
读取与导入只返回不可变 `MovieSnapshot`，插件拿不到任何可写 ORM
对象；受保护字段（插件可写白名单，见 v2-lite 设计文档）只能经
`context.movies.patch` 写入。

```python
from src.plugins import MovieSnapshot

# 读取（values 只含 MOVIE_SNAPSHOT_FIELDS 固定只读集合）
snapshot: MovieSnapshot | None = context.movies.get(movie_id)
snapshots: list[MovieSnapshot] = context.movies.find_by_numbers(["ABP-123", "IPX-456"])

# 写受保护字段（乐观并发）：
# - 字段未接管或 owner 是当前插件、且 revision 匹配 → 成功，返回 True
# - 否则整次零修改，返回 False（重新读取 snapshot 后决定是否重试）
ok = context.movies.patch(
    snapshot.movie_id,
    {"title": "插件补充标题"},
    expected_revision=snapshot.revision,
)
```

- `MovieSnapshot(movie_id, revision, values, owners)`：不可变；
  `revision` 是受保护字段版本，`owners` 是字段 -> `plugin:<id>` 接管映射；
- 插件要更新既有影片字段，必须先 `get`/`find_by_numbers` 取得 snapshot，
  再 `patch`；`import_movie_by_number` 保持纯新建语义，不覆盖已有影片；
- 字段写入白名单由宿主固定维护（当前开放 `title` / `summary` / `maker_name` /
  `director_name`，后续字段须由真实插件提出需求并收敛宿主写点后才开放），不在
  白名单内的字段调用 `patch` 会直接抛 `ValueError`；以上字段只接受字符串值；
- 插件被删除后其接管记录会保留在 `movie.field_owners`，字段冻结回宿主管理前
  需执行 `plugins clear-field-owners --plugin-id <id>` 解除接管。

## 7. 配置

```toml
[plugins]
root_dir = "/data/plugins"
enabled = ["subtitle_fetch"]

[plugins.job_crons.subtitle_fetch]
subtitle_fetch = "15 3 * * *"

[plugins.settings.subtitle_fetch]
overlap_days = 7

[plugins.settings.javdb_ranking]
javdb_username = "user@example.com"
javdb_password = "secret"
```

- `plugins.root_dir`：插件根目录，默认 `/data/plugins`；
- `plugins.enabled`：显式启用清单；不在清单里的插件不会被 import；
- `plugins.job_crons.<plugin_id>.<task_key>`：覆盖任务的默认 cron；
- `plugins.settings.<plugin_id>`：插件私有配置，`context.settings` 只读访问；
- 整个 `plugins` 节对通用配置 API（`GET/PATCH /config`）隐藏，
  安装/启停请用 `plugins` CLI，私有配置读写请用 `/system/plugins` 管理 API
  （前端「系统设置 → 插件」页即封装该 API），也可以直接编辑 `config.toml`；
- 配置与启停都在启动期生效，修改后需要重启 api 与 aps。

本地开发可以把 `root_dir` 指到 `./storage/plugins`，避免直接操作生产目录。

## 8. 依赖

插件**只能使用宿主 venv 已安装的包与标准库**（httpx、certifi、pydantic 等都在），
宿主不提供任何依赖安装能力。插件作者在发布前应确认依赖在宿主环境中可用；
如果将来有插件需要冷门包，再考虑增加最小依赖安装机制。

## 9. 安装与管理

### 9.1 插件根目录布局

```text
<plugins_root>/
  <plugin_id>/
    __init__.py
    manifest.json
    data/          # 宿主托管的运行数据（重装保留）
  .staging/        # 安装暂存区（zip 解压/上传临时文件，宿主自用）
```

### 9.2 CLI

| 命令 | 说明 |
|---|---|
| `plugins list` | 列出已安装插件与启停/加载状态 |
| `plugins install <目录或zip> [--sha256 ...] [--no-enable]` | 安装插件目录或 zip；已存在时替换代码并保留 `data/` |
| `plugins remove <id>` | 删除插件目录（含 `data/`，请先自行备份） |
| `plugins enable <id>` / `plugins disable <id>` | 启停（写入 `plugins.enabled`） |
| `plugins check <目录>` | 校验插件目录（import + register + 契约），供插件作者使用 |

安装方式等价于：把目录放到 `root_dir` 下、把 `plugin_id` 写进
`plugins.enabled`、重启 api 与 aps。`plugins install` / API 安装只是
这两步的便捷封装。

### 9.3 HTTP API（`/system/plugins`，需要登录鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/system/plugins` | 插件列表 |
| `GET` | `/system/plugins/{id}` | 插件详情 |
| `POST` | `/system/plugins` | multipart 上传 zip（`file` + 可选 `sha256` + `enable`），已存在时替换并保留 `data/` |
| `PATCH` | `/system/plugins/{id}?enabled=true` | 启停 |
| `DELETE` | `/system/plugins/{id}` | 删除插件目录（含 `data/`） |
| `GET` | `/system/plugins/{id}/settings` | 读取插件私有配置（`plugins.settings.<id>`） |
| `PUT` | `/system/plugins/{id}/settings` | 整体替换插件私有配置（JSON），不支持 null 值 |

安装/启停/删除/配置修改响应都会带 `pending_restart: ["api", "aps"]`。

### 9.4 生命周期要点

- **安装/升级**：替换目录即可；`data/` 会被保留。没有版本回滚，
  升级前请自行备份目录；
- **停用**：`plugins disable` 只从 `enabled` 移除，目录仍在根目录；
- **删除**：`plugins remove` 直接删除整个目录（含运行数据）；
- **重启**：插件在 import 期加载，所有安装/启停/删除操作后
  必须重启 **api 与 aps** 两个进程。

## 10. 安全模型与边界

- **同进程可信代码**：插件拥有与宿主相同的数据/网络/文件权限，
  只应安装可信来源的插件；
- **zip 有介质防护，无代码沙箱**：安装时拒绝越界路径/符号链接并限制大小，
  可选 sha256 校验，但代码仍按可信代码执行；
- 没有 import 白名单或沙箱：插件代码可以 import 任何模块，但请遵守
  本文描述的公开契约，不要绑定宿主内部实现；
- 插件不能直接写宿主数据库、不能注册 API/事件；
- `plugins.settings` 可能存放凭据：插件作者不要把示例配置里的敏感值
  提交进公开仓库，也不要要求部署者把凭据写死在插件代码里。

## 11. 开发与调试

### 11.1 本地开发配置

```toml
[plugins]
root_dir = "./storage/plugins"
enabled = ["subtitle_fetch"]
```

把插件目录放到 `storage/plugins/<plugin_id>/` 后启动服务即可加载；
改代码后重启 api/aps。

### 11.2 常见失败排查

| 症状 | 原因与处理 |
|---|---|
| zip/API 安装被拒 | 介质校验或试加载失败：看错误 stage（zip 校验、manifest、requires_python、import、register、契约等） |
| 启动后任务不在 `GET /system/jobs` | 插件未启用、目录缺 manifest/`__init__.py`、register 报错或任务 key 冲突；`plugins list` 看 `load_status` |
| `plugins check` 报错 | register 抛异常或返回的契约不合法；按 stage 提示修复 |
| `409 task_conflict` | 同一任务已在运行；等它结束或处理完成后重试 |
| 定时任务被跳过 | 同一 task_key 正在运行，按 coalesce 丢弃本次触发 |
| 插件 import 报错 | 使用了宿主没有的第三方包；改成宿主 venv 已有依赖或标准库 |

### 11.3 仓库内参考实现与测试

- 契约与公开类型：`src/plugins/contracts.py`、`src/plugins/context.py`、`src/plugins/types.py`；
- 扩展点目录与排行榜领域：`src/plugins/extensions/`、`src/plugins/extensions/ranking.py`；
- 任务模型：`src/scheduler/contracts.py`；
- 加载与隔离：`src/plugins/loader.py`；
- zip 安全解压：`src/plugins/installer.py`；
- 目录管理：`src/plugins/manager.py`；
- 管理接口：`src/api/routers/system/plugins.py`；
- 排行榜装配：`src/scheduler/ranking_plugin_adapter.py`；
- 测试：`tests/start/test_plugins.py`、`tests/start/test_plugin_manager.py`、
  `tests/api/test_plugins_api.py`。

## 12. 常见问题

**插件一定要注册任务吗？**

不强制：`jobs=()` 是合法声明。零任务插件可以只声明扩展点
（当前唯一扩展点是 `discovery.ranking_source`），或完全不声明
（register 仅产生副作用，不建议作为主要插件形态）。

**插件的 `settings` 能修改吗？**

不能。`context.settings` 是深冻结只读结构（嵌套 dict/list 也不可改），
修改只能由部署者改 `config.toml` 后重启。

**插件可以有自己的数据库吗？**

可以，但不能直接访问宿主数据库。建议用 `context.data_dir`
存文件型数据（SQLite、JSON 等），或访问外部数据库服务。

**宿主升级会破坏我的插件吗？**

`host_api_version` 区间校验提供了兼容信号：契约只增不减时提高
`HOST_API_VERSION`；破坏性行为变更不会强制提高 `MIN_SUPPORTED_HOST_API_VERSION`
（v1 插件仍可加载，但运行期行为按最新版本语义执行，见 6.8 的 v2 说明）。
插件应只依赖本文描述的公开契约，不绑定宿主内部实现。

**插件任务会并发运行吗？**

同一个 `task_key` 同时只会运行一个实例：定时触发在已有排队/运行实例时丢弃，
手动触发与运行中实例冲突时返回 `409`。不同插件、不同 task_key 之间互不影响。
