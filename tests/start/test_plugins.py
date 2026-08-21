"""插件机制护栏测试：目录插件加载、契约、扩展点、注册表与参数化任务。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from src.config.config import Plugins
from src.plugins import HOST_API_VERSION
from src.plugins.contracts import (
    PluginExtension,
    PluginRegistration,
)
from src.plugins.extensions.ranking import (
    RANKING_SOURCE_EXTENSION_KEY,
    PluginRankingBoard,
    PluginRankingSource,
)
from src.plugins.loader import (
    PLUGIN_LOAD_ERRORS,
    check_plugin_dir,
    load_enabled_plugins,
)
from src.scheduler.contracts import JobDefinition
from src.scheduler.job_execution_adapter import (
    JobExecutionResolutionError,
    resolve_job_definition_handler,
    resolve_job_execution,
)
from src.scheduler.queue_tasks import QueueTaskDefinition
from src.scheduler.ranking_plugin_adapter import apply_plugin_ranking_sources
from src.scheduler.registry import _build_job_registry
from src.service.discovery.ranking_service import (
    RANKING_SOURCE_OWNERS,
    RANKING_SOURCES,
)
from src.start.aps import get_job_cron_setting, resolve_job_cron_expr


def _write_plugin_dir(
    base: Path,
    plugin_id: str,
    *,
    version: str = "1.0.0",
    init_source: str = "",
) -> Path:
    pkg = base / plugin_id
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": plugin_id,
                "version": version,
                "host_api_version": HOST_API_VERSION,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(init_source, encoding="utf-8")
    return pkg


def _empty_register_source() -> str:
    return (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='__PLUGIN_ID__', display_name='x', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, jobs=())\n"
    )


@pytest.fixture(autouse=True)
def _clear_load_errors():
    PLUGIN_LOAD_ERRORS.clear()
    yield
    PLUGIN_LOAD_ERRORS.clear()


def test_loader_loads_plugin_with_params_jobs(tmp_path):
    init_source = """\
from pydantic import BaseModel
from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration
from src.scheduler.contracts import JobDefinition

class FetchParams(BaseModel):
    movie_number: str

def register(context: PluginContext) -> PluginRegistration:
    def run(reporter):
        return {"ok": True}
    def fetch(reporter, params):
        return params
    return PluginRegistration(
        plugin_id="demo_plugin",
        display_name="演示插件",
        version="1.0.0",
        host_api_version=HOST_API_VERSION,
        jobs=(
            JobDefinition(
                task_key="demo_sync",
                log_name="demo-sync",
                cli_name="demo-sync",
                cli_help="演示定时任务",
                default_cron="0 5 * * *",
                service_factory=run,
            ),
            JobDefinition(
                task_key="demo_fetch",
                log_name="demo-fetch",
                cli_name="demo-fetch",
                cli_help="演示带参任务",
                manual_only=True,
                params_schema=FetchParams,
                params_handler=fetch,
            ),
        ),
    )
"""
    root = tmp_path / "root"
    _write_plugin_dir(root, "demo_plugin", init_source=init_source)

    loaded = load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=root)
    assert [registration.plugin_id for registration in loaded] == ["demo_plugin"]
    registration = loaded[0]
    assert {job.task_key for job in registration.jobs} == {
        "demo_sync",
        "demo_fetch",
    }
    by_key = {job.task_key: job for job in registration.jobs}
    assert by_key["demo_sync"].plugin_id == "demo_plugin"
    assert by_key["demo_fetch"].manual_only is True
    assert by_key["demo_fetch"].params_schema is not None
    assert by_key["demo_fetch"].params_schema.__name__ == "FetchParams"
    assert by_key["demo_fetch"].params_schema.model_json_schema()["properties"][
        "movie_number"
    ]
    assert get_job_cron_setting(by_key["demo_fetch"]) is None
    assert resolve_job_cron_expr(by_key["demo_fetch"]) is None
    assert (root / "demo_plugin" / "data").is_dir()
    assert PLUGIN_LOAD_ERRORS == {}


def test_loader_isolates_broken_plugin(tmp_path):
    root = tmp_path / "root"
    bad_source = (
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    raise RuntimeError('boom')\n"
    )
    _write_plugin_dir(root, "bad_plugin", init_source=bad_source)
    _write_plugin_dir(
        root,
        "good_plugin",
        init_source=_empty_register_source().replace("__PLUGIN_ID__", "good_plugin"),
    )

    loaded = load_enabled_plugins(
        Plugins(enabled=["bad_plugin", "good_plugin"]),
        root_dir=root,
    )
    assert [registration.plugin_id for registration in loaded] == ["good_plugin"]
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "register"


def test_loader_clears_stale_error_on_success(tmp_path):
    root = tmp_path / "root"
    bad_source = (
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    raise RuntimeError('boom')\n"
    )
    _write_plugin_dir(root, "demo_plugin", init_source=bad_source)
    load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["demo_plugin"]["stage"] == "register"

    # 修复插件后再次加载：错误应被清除，且不残留旧模块状态。
    good_source = _empty_register_source().replace("__PLUGIN_ID__", "demo_plugin")
    (root / "demo_plugin" / "__init__.py").write_text(good_source, encoding="utf-8")
    loaded = load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=root)
    assert [registration.plugin_id for registration in loaded] == ["demo_plugin"]
    assert PLUGIN_LOAD_ERRORS == {}


def test_registration_version_mismatch_rejected(tmp_path):
    root = tmp_path / "root"
    source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='demo_plugin', display_name='x', "
        "version='2.0.0', host_api_version=HOST_API_VERSION, jobs=())\n"
    )
    _write_plugin_dir(root, "demo_plugin", init_source=source)

    load_enabled_plugins(Plugins(enabled=["demo_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["demo_plugin"]["stage"] == "validate_registration"


def test_plugin_settings_are_deeply_readonly(tmp_path):
    root = tmp_path / "root"
    good_source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='demo_plugin', display_name="
        "str(context.settings['scrape']['overlap_days']), version='1.0.0', "
        "host_api_version=HOST_API_VERSION, jobs=())\n"
    )
    _write_plugin_dir(root, "demo_plugin", init_source=good_source)
    plugin_settings = Plugins(
        enabled=["demo_plugin"],
        settings={"demo_plugin": {"scrape": {"overlap_days": 7}}},
    )
    loaded = load_enabled_plugins(plugin_settings, root_dir=root)
    assert loaded[0].display_name == "7"

    # 嵌套 dict 也禁止修改：插件尝试写入应导致 register 阶段失败。
    bad_source = good_source.replace(
        "return PluginRegistration(",
        "context.settings['scrape']['overlap_days'] = 1\n    return PluginRegistration(",
    )
    (root / "demo_plugin" / "__init__.py").write_text(bad_source, encoding="utf-8")
    load_enabled_plugins(plugin_settings, root_dir=root)
    assert PLUGIN_LOAD_ERRORS["demo_plugin"]["stage"] == "register"


def test_check_plugin_dir_validates_and_cleans_modules(tmp_path):
    good_source = _empty_register_source().replace("__PLUGIN_ID__", "demo_plugin")
    plugin_dir = _write_plugin_dir(tmp_path, "demo_plugin", init_source=good_source)

    registration = check_plugin_dir(plugin_dir=plugin_dir)
    assert registration.plugin_id == "demo_plugin"
    import sys

    assert not any(
        name.startswith("sakuramedia_plugins.demo_plugin")
        for name in sys.modules
    )

    bad_source = (
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    raise RuntimeError('boom')\n"
    )
    (plugin_dir / "__init__.py").write_text(bad_source, encoding="utf-8")
    with pytest.raises(RuntimeError, match="register"):
        check_plugin_dir(plugin_dir=plugin_dir)


def test_job_params_validation_rules():
    with pytest.raises(ValidationError):
        JobDefinition(
            task_key="x",
            log_name="x",
            cli_name="x",
            cli_help="x",
            manual_only=True,
            default_cron="0 5 * * *",
            service_factory=lambda reporter: None,
        )
    with pytest.raises(ValidationError):
        JobDefinition(
            task_key="x",
            log_name="x",
            cli_name="x",
            cli_help="x",
            default_cron="0 5 * * *",
            service_factory=lambda reporter: None,
            params_schema=BaseModel,
        )


def test_job_execution_adapter_preserves_mixed_plugin_null_vs_empty_params():
    calls = []

    class EmptyParams(BaseModel):
        pass

    job_def = JobDefinition(
        task_key="demo_mixed",
        log_name="demo-mixed",
        cli_name="demo-mixed",
        cli_help="mixed",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: calls.append(("factory", reporter)) or {},
        params_schema=EmptyParams,
        params_handler=lambda reporter, params: (
            calls.append(("params", reporter, params)) or {}
        ),
    ).model_copy(update={"plugin_id": "demo_plugin"})
    identity = (
        job_def.task_key,
        job_def.cli_name,
        job_def.log_name,
        job_def.plugin_id,
    )
    reporter = object()

    no_params = resolve_job_execution(
        task_key=job_def.task_key,
        raw_params=None,
        job_def=job_def,
        queue_def=None,
    )
    explicit_empty = resolve_job_execution(
        task_key=job_def.task_key,
        raw_params=EmptyParams.model_validate({}).model_dump(),
        job_def=job_def,
        queue_def=None,
    )
    no_params.func(reporter)
    explicit_empty.func(reporter)

    assert calls == [("factory", reporter), ("params", reporter, {})]
    # adapter 只包装 handler，不得重写社区插件的稳定任务标识。
    assert (
        job_def.task_key,
        job_def.cli_name,
        job_def.log_name,
        job_def.plugin_id,
    ) == identity


def test_job_execution_adapter_manual_only_empty_model_uses_params_handler():
    calls = []

    class EmptyParams(BaseModel):
        pass

    job_def = JobDefinition(
        task_key="demo_manual_empty",
        log_name="demo-manual-empty",
        cli_name="demo-manual-empty",
        cli_help="manual empty",
        manual_only=True,
        params_schema=EmptyParams,
        params_handler=lambda reporter, params: calls.append(params) or {},
    ).model_copy(update={"plugin_id": "demo_plugin"})

    execution = resolve_job_execution(
        task_key=job_def.task_key,
        raw_params={},
        job_def=job_def,
        queue_def=None,
    )
    execution.func(object())

    assert calls == [{}]
    assert execution.log_name == "demo-manual-empty"
    assert execution.notify_result is True


def test_job_definition_handler_nonempty_params_truth_table():
    calls = []

    class ValueParams(BaseModel):
        value: int

    params_only = JobDefinition(
        task_key="params_only_nonempty",
        log_name="params-only-nonempty",
        cli_name="params-only-nonempty",
        cli_help="params only nonempty",
        manual_only=True,
        params_schema=ValueParams,
        params_handler=lambda reporter, params: (
            calls.append(("params_only", params)) or {}
        ),
    )
    mixed = JobDefinition(
        task_key="mixed_nonempty",
        log_name="mixed-nonempty",
        cli_name="mixed-nonempty",
        cli_help="mixed nonempty",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: calls.append(("factory", None)) or {},
        params_schema=ValueParams,
        params_handler=lambda reporter, params: calls.append(("mixed", params)) or {},
    )
    factory_only = JobDefinition(
        task_key="factory_only_nonempty",
        log_name="factory-only-nonempty",
        cli_name="factory-only-nonempty",
        cli_help="factory only nonempty",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: {},
    )
    params = ValueParams(value=7).model_dump()

    resolve_job_definition_handler(job_def=params_only, raw_params=params)(object())
    resolve_job_definition_handler(job_def=mixed, raw_params=params)(object())

    assert calls == [("params_only", {"value": 7}), ("mixed", {"value": 7})]
    with pytest.raises(JobExecutionResolutionError, match="不支持带参执行"):
        resolve_job_definition_handler(job_def=factory_only, raw_params=params)


def test_job_execution_adapter_queue_override_keeps_existing_priority():
    calls = []
    job_def = JobDefinition(
        task_key="builtin_overlap",
        log_name="builtin-overlap",
        cli_name="builtin-overlap",
        cli_help="overlap",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: calls.append("factory") or {},
    )
    queue_def = QueueTaskDefinition(
        task_key=job_def.task_key,
        log_name="builtin-overlap-subset",
        handler=lambda reporter, params: calls.append(("queue", params)) or {},
        notify_result=False,
    )

    explicit_empty = resolve_job_execution(
        task_key=job_def.task_key,
        raw_params={},
        job_def=job_def,
        queue_def=queue_def,
    )
    no_params = resolve_job_execution(
        task_key=job_def.task_key,
        raw_params=None,
        job_def=job_def,
        queue_def=queue_def,
    )
    explicit_empty.func(object())
    no_params.func(object())

    assert calls == [("queue", {}), "factory"]
    assert explicit_empty.log_name == "builtin-overlap-subset"
    assert explicit_empty.notify_result is False


def test_job_execution_adapter_queue_only_accepts_null_empty_and_nonempty_params():
    calls = []
    queue_def = QueueTaskDefinition(
        task_key="queue_only",
        log_name="queue-only",
        handler=lambda reporter, params: calls.append(params.copy()) or {},
    )

    for raw_params in (None, {}, {"value": 7}):
        execution = resolve_job_execution(
            task_key=queue_def.task_key,
            raw_params=raw_params,
            job_def=None,
            queue_def=queue_def,
        )
        execution.func(object())

    assert calls == [{}, {}, {"value": 7}]


def test_job_execution_adapter_rejects_missing_legal_handler():
    class EmptyParams(BaseModel):
        pass

    params_only = JobDefinition(
        task_key="params_only",
        log_name="params-only",
        cli_name="params-only",
        cli_help="params only",
        manual_only=True,
        params_schema=EmptyParams,
        params_handler=lambda reporter, params: {},
    )
    factory_only = JobDefinition(
        task_key="factory_only",
        log_name="factory-only",
        cli_name="factory-only",
        cli_help="factory only",
        default_cron="0 5 * * *",
        service_factory=lambda reporter: {},
    )

    with pytest.raises(JobExecutionResolutionError, match="缺少无参执行体"):
        resolve_job_execution(
            task_key=params_only.task_key,
            raw_params=None,
            job_def=params_only,
            queue_def=None,
        )
    with pytest.raises(JobExecutionResolutionError, match="不支持带参执行"):
        resolve_job_execution(
            task_key=factory_only.task_key,
            raw_params={},
            job_def=factory_only,
            queue_def=None,
        )
    with pytest.raises(JobExecutionResolutionError, match="未在注册表"):
        resolve_job_execution(
            task_key="missing",
            raw_params={"value": 7},
            job_def=None,
            queue_def=None,
        )


def test_host_api_version_range_enforced():
    from src.plugins.contracts import PluginRegistration

    with pytest.raises(ValidationError):
        PluginRegistration(
            plugin_id="x",
            display_name="x",
            version="1.0.0",
            host_api_version=HOST_API_VERSION + 1,
        )


def test_manifest_host_api_version_range_enforced(tmp_path):
    """manifest 是版本唯一声明入口：声明越界直接拒绝加载（register 默认值会漂移）。"""
    import json

    from src.config.config import Plugins
    from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins

    plugin_id = "v3_plugin"
    pkg = tmp_path / plugin_id
    pkg.mkdir()
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": "v3",
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION + 1,
            }
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        f"    return PluginRegistration(plugin_id={plugin_id!r}, display_name='v3', version='1.0.0', jobs=())\n",
        encoding="utf-8",
    )

    loaded = load_enabled_plugins(
        Plugins(enabled=[plugin_id]),
        root_dir=tmp_path,
    )
    assert loaded == ()
    assert PLUGIN_LOAD_ERRORS[plugin_id]["stage"] == "validate_registration"


def test_manifest_register_version_mismatch_accepts_manifest(tmp_path):
    """register 不显式传 host_api_version 时默认跟随宿主（v1 老插件漂移为 2）：

    manifest 声明 1 仍以 manifest 为准正常加载，不静默拒绝（运行期行为按 v2 语义）。
    """
    import json

    from src.config.config import Plugins
    from src.plugins.loader import PLUGIN_LOAD_ERRORS, load_enabled_plugins

    plugin_id = "v1_legacy"
    pkg = tmp_path / plugin_id
    pkg.mkdir()
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": plugin_id,
                "display_name": "legacy",
                "version": "1.0.0",
                "host_api_version": 1,
            }
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(
        "from src.plugins import PluginContext, PluginRegistration\n"
        "def register(context):\n"
        "    # v1 时代的老插件不显式声明 host_api_version（默认跟随宿主）。\n"
        f"    return PluginRegistration(plugin_id={plugin_id!r}, display_name='legacy', version='1.0.0', jobs=())\n",
        encoding="utf-8",
    )

    loaded = load_enabled_plugins(
        Plugins(enabled=[plugin_id]),
        root_dir=tmp_path,
    )
    assert [registration.plugin_id for registration in loaded] == [plugin_id]
    assert PLUGIN_LOAD_ERRORS == {}


def test_registry_skips_plugin_job_conflicting_with_queue_key():
    def run(reporter):
        return {}

    registration = PluginRegistration(
        plugin_id="bad_plugin",
        display_name="bad",
        version="1.0.0",
        jobs=(
            JobDefinition(
                task_key="library_import",
                log_name="bad-job",
                cli_name="bad-job",
                cli_help="x",
                default_cron="0 5 * * *",
                service_factory=run,
            ),
        ),
    )
    jobs = _build_job_registry([], (registration,))
    assert all(job.plugin_id != "bad_plugin" for job in jobs)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "registry_conflict"


def test_registry_isolates_plugin_job_conflicting_with_builtin():
    def run(reporter):
        return {}

    builtin = JobDefinition(
        task_key="builtin_x",
        log_name="builtin-x",
        cli_name="builtin-x",
        cli_help="x",
        cron_setting="movie_heat_cron",
        service_factory=run,
    )
    plugin_job = JobDefinition(
        task_key="builtin_x",
        log_name="plugin-x",
        cli_name="plugin-x",
        cli_help="x",
        default_cron="0 5 * * *",
        service_factory=run,
    ).model_copy(update={"plugin_id": "bad_plugin"})
    registration = PluginRegistration(
        plugin_id="bad_plugin",
        display_name="bad",
        version="1.0.0",
        jobs=(plugin_job,),
    )
    jobs = _build_job_registry([builtin], (registration,))
    assert [job.task_key for job in jobs] == ["builtin_x"]
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "registry_conflict"


def test_plugin_extension_validates_shape():
    # key 必须符合点分命名空间格式
    with pytest.raises(ValidationError):
        PluginExtension(key="Bad.Key", data={"x": 1})
    # data 不能为空
    with pytest.raises(ValidationError):
        PluginExtension(key="discovery.ranking_source", data=None)
    # 合法声明通过
    PluginExtension(key="discovery.ranking_source", data={"x": 1})


def test_loader_rejects_unknown_extension_key(tmp_path):
    root = tmp_path / "root"
    source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginExtension, "
        "PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='bad_plugin', display_name='x', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, "
        "extensions=(PluginExtension(key='foo.bar', data={'x': 1}),))\n"
    )
    _write_plugin_dir(root, "bad_plugin", init_source=source)

    load_enabled_plugins(Plugins(enabled=["bad_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "validate_extensions"


def test_loader_rejects_duplicate_extension_key(tmp_path):
    root = tmp_path / "root"
    source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginExtension, "
        "PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='bad_plugin', display_name='x', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, "
        "extensions=(PluginExtension(key='foo.bar', data={'x': 1}), "
        "PluginExtension(key='foo.bar', data={'y': 2})))\n"
    )
    _write_plugin_dir(root, "bad_plugin", init_source=source)

    load_enabled_plugins(Plugins(enabled=["bad_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "validate_extensions"


def test_loader_rejects_bad_ranking_extension_payload(tmp_path):
    root = tmp_path / "root"
    source = (
        "from src.plugins import HOST_API_VERSION, PluginContext, PluginExtension, "
        "PluginRegistration, RANKING_SOURCE_EXTENSION_KEY\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='bad_plugin', display_name='x', "
        "version='1.0.0', host_api_version=HOST_API_VERSION, "
        "extensions=(PluginExtension(key=RANKING_SOURCE_EXTENSION_KEY, "
        "data={'source_key': 'bad', 'name': 'x', 'boards': []}),))\n"
    )
    _write_plugin_dir(root, "bad_plugin", init_source=source)

    load_enabled_plugins(Plugins(enabled=["bad_plugin"]), root_dir=root)
    assert PLUGIN_LOAD_ERRORS["bad_plugin"]["stage"] == "validate_extensions"


def test_plugin_ranking_board_validates_periods():
    # 静态与动态周期二选一
    with pytest.raises(ValidationError):
        PluginRankingBoard(
            key="hot",
            name="hot",
            supported_periods=("daily",),
            supported_periods_provider=lambda: ("daily",),
            fetch_numbers=lambda period: [],
        )
    # default_period 必须属于静态周期
    with pytest.raises(ValidationError):
        PluginRankingBoard(
            key="hot",
            name="hot",
            supported_periods=("daily",),
            default_period="weekly",
            fetch_numbers=lambda period: [],
        )
    # 单期榜不允许 default_period
    with pytest.raises(ValidationError):
        PluginRankingBoard(
            key="hot",
            name="hot",
            default_period="daily",
            fetch_numbers=lambda period: [],
        )
    # 合法声明通过
    PluginRankingBoard(
        key="hot",
        name="hot",
        supported_periods=("daily", "weekly"),
        default_period="daily",
        fetch_numbers=lambda period: [],
    )


def test_apply_plugin_ranking_sources_merges_and_isolates_conflicts():
    def fetch_a(period):
        return ["ABP-123"]

    def fetch_b(period):
        return ["IPX-456"]

    reg_a = PluginRegistration(
        plugin_id="rank_a",
        display_name="A",
        version="1.0.0",
        extensions=(
            PluginExtension(
                key=RANKING_SOURCE_EXTENSION_KEY,
                data=PluginRankingSource(
                    source_key="aaa",
                    name="AAA",
                    boards=(
                        PluginRankingBoard(
                            key="hot",
                            name="hot",
                            fetch_numbers=fetch_a,
                        ),
                    ),
                ),
            ),
        ),
    )
    reg_b = PluginRegistration(
        plugin_id="rank_b",
        display_name="B",
        version="1.0.0",
        extensions=(
            PluginExtension(
                key=RANKING_SOURCE_EXTENSION_KEY,
                data=PluginRankingSource(
                    source_key="aaa",
                    name="AAA2",
                    boards=(
                        PluginRankingBoard(
                            key="hot",
                            name="hot",
                            fetch_numbers=fetch_b,
                        ),
                    ),
                ),
            ),
            PluginExtension(
                key=RANKING_SOURCE_EXTENSION_KEY,
                data=PluginRankingSource(
                    source_key="bbb",
                    name="BBB",
                    boards=(
                        PluginRankingBoard(
                            key="hot",
                            name="hot",
                            fetch_numbers=fetch_b,
                        ),
                    ),
                ),
            ),
        ),
    )

    rejected = apply_plugin_ranking_sources((reg_a, reg_b))
    assert rejected == {"rank_b"}
    assert set(RANKING_SOURCES) == {"aaa"}
    assert RANKING_SOURCE_OWNERS == {"aaa": "rank_a"}
    assert RANKING_SOURCES["aaa"].plugin_id == "rank_a"
    assert RANKING_SOURCES["aaa"].boards[0].fetch_numbers("daily") == ["ABP-123"]
    assert PLUGIN_LOAD_ERRORS["rank_b"]["stage"] == "ranking_conflict"

    # 空插件集合恢复空注册表（排行榜不是默认功能）
    apply_plugin_ranking_sources(())
    assert RANKING_SOURCES == {}
    assert RANKING_SOURCE_OWNERS == {}
