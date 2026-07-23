# 仓库内插件机制

插件机制用于把可选能力隔离在 `src/plugins/extensions/`，同时复用 SakuraMedia 的任务调度、
活动流以及受控宿主能力。插件属于与主程序同进程运行的可信代码，不是安全沙箱。

## 启用与禁用

插件必须在 `/data/config/config.toml` 中显式启用：

```toml
[plugins]
enabled = ["javbooks"]

[plugins.job_crons.javbooks]
javbooks_full_scrape = "0 5 * * 1"

[plugins.settings.javbooks]
# 由插件自行定义并用 Pydantic 校验
```

- 只有 `enabled` 中的插件会被导入；仅存在代码或配置不会启用插件。
- 插件 ID 只允许小写字母、数字、下划线，并且必须以字母开头。
- 启停或修改插件配置后需要重启整个 `sakuramedia` 服务，使 API 与 APS 进程加载同一注册表。
- `plugins` 节不通过通用 `/config` 接口读取或修改，避免泄露插件私有配置或制造热更新假象。
- 禁用插件不会删除已有 `BackgroundTaskRun` 历史记录。

## 目录和入口

```text
src/plugins/
  contracts.py
  context.py
  loader.py
  extensions/
    <plugin_id>/
      __init__.py
```

loader 只会导入固定路径 `src.plugins.extensions.<plugin_id>`，不接受任意模块路径。插件包必须
在 `__init__.py` 中暴露：

```python
def register(context: PluginContext) -> PluginRegistration:
    ...
```

`register()` 只负责校验配置和声明任务。禁止在注册阶段访问数据库、发起网络请求、创建线程或
执行实际业务；业务必须放进任务的 `service_factory`。

## 注册示例

```python
from pydantic import BaseModel

from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration
from src.scheduler.contracts import JobDefinition


class ExampleSettings(BaseModel):
    batch_size: int = 20


def register(context: PluginContext) -> PluginRegistration:
    config = ExampleSettings.model_validate(context.settings)

    def run(reporter):
        provider = context.build_javdb_provider()
        importer = context.build_catalog_import_service()
        # 业务循环由插件实现，并通过 reporter 上报进度。
        return {"batch_size": config.batch_size}

    return PluginRegistration(
        plugin_id="example",
        display_name="示例插件",
        version="1.0.0",
        host_api_version=HOST_API_VERSION,
        jobs=(
            JobDefinition(
                task_key="example_sync",
                log_name="example-sync",
                cli_name="sync-example",
                cli_help="执行一次示例同步",
                default_cron="0 5 * * 1",
                service_factory=run,
            ),
        ),
    )
```

插件任务不能设置 `cron_setting` 或 `plugin_id`：

- `cron_setting` 是内建任务专用的 `Scheduler` 静态字段。
- `plugin_id` 由 loader 根据显式启用项注入，插件不能伪造来源。
- 插件必须声明合法的 `default_cron`；`plugins.job_crons.<plugin_id>.<task_key>` 可覆盖它。

## 自动接入的能力

插件任务成功注册后会自动进入：

- APScheduler cron 调度；
- `uv run python -m src.start.commands aps <cli_name>`；
- `GET /system/jobs`；
- `POST /system/jobs/{task_key}/run`；
- `ActivityService` 的任务记录、进度、通知、恢复和 `aps:<task_key>` 互斥。

全局范围内 `task_key`、`cli_name`、`log_name` 都必须唯一，包含内建任务和其他插件任务。
冲突会在启动阶段直接报错，不发布半成品注册表。

## 宿主能力

`PluginContext` 是受支持的插件 API，目前提供：

- `build_javdb_provider()`；
- `build_catalog_import_service()`；
- `import_movie_by_number(...)`；
- `get_task_logger(...)`；
- 当前插件的只读配置视图 `context.settings`。

批量处理应分别构造并复用 provider/importer，避免逐番号重复创建客户端。插件技术上仍可直接
导入 `src.service.*`，但这不属于稳定插件 API，核心重构不保证兼容。

## 错误和安全边界

已启用插件出现导入失败、契约不符、配置非法、Host API 版本不兼容或任务冲突时，API/APS
进程会 fail-fast。未启用插件不会被导入，其代码错误不会影响启动。

插件拥有与主程序相同的数据库、网络和文件系统权限。因此不提供 Web 上传、URL 下载、任意
目录加载、运行时安装依赖或热加载。插件新增依赖仍需正常修改 `pyproject.toml` 并更新锁文件。
