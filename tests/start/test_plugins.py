from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.api.exception.errors import ApiError
from src.api.routers.system.jobs import _build_job_metadata
from src.config.config import Plugins
from src.plugins.context import PluginContext
from src.plugins.contracts import HOST_API_VERSION, PluginRegistration
from src.plugins.loader import PluginLoadError, load_enabled_plugins
from src.scheduler.contracts import JobDefinition
from src.scheduler.registry import _build_job_registry
from src.service.system.config_service import ConfigService
from src.start.aps import get_job_cron_setting, resolve_job_cron_expr


def _build_job(
    task_key: str,
    *,
    cli_name: str | None = None,
    log_name: str | None = None,
) -> JobDefinition:
    return JobDefinition(
        task_key=task_key,
        log_name=log_name or task_key.replace("_", "-"),
        cli_name=cli_name or task_key.replace("_", "-"),
        cli_help=f"执行 {task_key}",
        default_cron="0 5 * * 1",
        service_factory=lambda _reporter: {"ok": True},
    )


def _build_registration(
    plugin_id: str,
    *jobs: JobDefinition,
    host_api_version: int = HOST_API_VERSION,
) -> PluginRegistration:
    return PluginRegistration(
        plugin_id=plugin_id,
        display_name=plugin_id,
        version="1.0.0",
        host_api_version=host_api_version,
        jobs=jobs,
    )


def test_disabled_plugin_is_not_imported():
    imported_modules = []

    loaded = load_enabled_plugins(
        Plugins(),
        module_importer=lambda module_name: imported_modules.append(module_name),
    )

    assert loaded == ()
    assert imported_modules == []


def test_enabled_plugins_load_in_config_order_and_receive_own_settings():
    received = {}

    def importer(module_name):
        plugin_id = module_name.rsplit(".", 1)[-1]

        def register(context):
            received[plugin_id] = (context.plugin_id, dict(context.settings))
            return _build_registration(plugin_id, _build_job(f"{plugin_id}_job"))

        return SimpleNamespace(register=register)

    loaded = load_enabled_plugins(
        Plugins(
            enabled=["first", "second"],
            settings={
                "first": {"page_size": 20},
                "second": {"page_size": 50},
            },
        ),
        module_importer=importer,
    )

    assert [plugin.plugin_id for plugin in loaded] == ["first", "second"]
    assert received == {
        "first": ("first", {"page_size": 20}),
        "second": ("second", {"page_size": 50}),
    }
    assert loaded[0].jobs[0].plugin_id == "first"
    assert loaded[1].jobs[0].plugin_id == "second"


@pytest.mark.parametrize(
    ("module", "stage"),
    [
        (SimpleNamespace(), "resolve_register"),
        (
            SimpleNamespace(register=lambda _context: {"plugin_id": "broken"}),
            "register",
        ),
    ],
)
def test_enabled_plugin_contract_errors_fail_fast(module, stage):
    with pytest.raises(PluginLoadError) as exc_info:
        load_enabled_plugins(
            Plugins(enabled=["broken"]),
            module_importer=lambda _module_name: module,
        )

    assert exc_info.value.plugin_id == "broken"
    assert exc_info.value.stage == stage


def test_enabled_plugin_import_error_preserves_cause():
    def importer(_module_name):
        raise ImportError("boom")

    with pytest.raises(PluginLoadError) as exc_info:
        load_enabled_plugins(
            Plugins(enabled=["broken"]),
            module_importer=importer,
        )

    assert exc_info.value.stage == "import"
    assert isinstance(exc_info.value.__cause__, ImportError)


def test_plugin_id_and_host_api_version_must_match():
    mismatch_id_module = SimpleNamespace(
        register=lambda _context: _build_registration(
            "another",
            _build_job("another_job"),
        )
    )
    with pytest.raises(PluginLoadError, match="与配置不一致"):
        load_enabled_plugins(
            Plugins(enabled=["expected"]),
            module_importer=lambda _module_name: mismatch_id_module,
        )

    mismatch_version_module = SimpleNamespace(
        register=lambda _context: _build_registration(
            "expected",
            _build_job("expected_job"),
            host_api_version=HOST_API_VERSION + 1,
        )
    )
    with pytest.raises(PluginLoadError, match="Host API 版本不兼容"):
        load_enabled_plugins(
            Plugins(enabled=["expected"]),
            module_importer=lambda _module_name: mismatch_version_module,
        )


def test_plugin_job_cannot_reference_host_scheduler_field():
    module = SimpleNamespace(
        register=lambda _context: _build_registration(
            "sample",
            JobDefinition(
                task_key="sample_job",
                log_name="sample-job",
                cli_name="sample-job",
                cli_help="sample",
                cron_setting="ranking_sync_cron",
                service_factory=lambda _reporter: None,
            ),
        )
    )

    with pytest.raises(PluginLoadError, match="不允许引用宿主 Scheduler 字段"):
        load_enabled_plugins(
            Plugins(enabled=["sample"]),
            module_importer=lambda _module_name: module,
        )


def test_unknown_plugin_cron_override_fails_fast():
    module = SimpleNamespace(
        register=lambda _context: _build_registration(
            "sample",
            _build_job("sample_job"),
        )
    )

    with pytest.raises(PluginLoadError, match="未注册的任务"):
        load_enabled_plugins(
            Plugins(
                enabled=["sample"],
                job_crons={"sample": {"misspelled_job": "0 1 * * *"}},
            ),
            module_importer=lambda _module_name: module,
        )


def test_registry_rejects_task_cli_and_log_name_collisions():
    core_job = _build_job("core_job")
    for conflicting_job in (
        _build_job("core_job", cli_name="plugin-command", log_name="plugin-log"),
        _build_job("plugin_job", cli_name=core_job.cli_name, log_name="plugin-log"),
        _build_job("plugin_job", cli_name="plugin-command", log_name=core_job.log_name),
    ):
        plugin_job = conflicting_job.model_copy(update={"plugin_id": "sample"})
        registration = _build_registration("sample", plugin_job)
        with pytest.raises(RuntimeError, match="任务注册冲突"):
            _build_job_registry([core_job], (registration,))


def test_plugin_cron_uses_default_and_explicit_override(monkeypatch):
    job = _build_job("sample_job").model_copy(update={"plugin_id": "sample"})

    monkeypatch.setattr(
        "src.start.aps.settings.plugins",
        Plugins(enabled=["sample"]),
    )
    assert resolve_job_cron_expr(job) == "0 5 * * 1"
    assert get_job_cron_setting(job) == "plugins.job_crons.sample.sample_job"

    monkeypatch.setattr(
        "src.start.aps.settings.plugins",
        Plugins(
            enabled=["sample"],
            job_crons={"sample": {"sample_job": "30 3 * * *"}},
        ),
    )
    assert resolve_job_cron_expr(job) == "30 3 * * *"

    metadata = _build_job_metadata(job, None)
    assert metadata.plugin_id == "sample"
    assert metadata.cron_setting == "plugins.job_crons.sample.sample_job"
    assert metadata.cron_expr == "30 3 * * *"


def test_plugin_context_import_movie_by_number_reuses_host_flow(monkeypatch):
    calls = []
    detail = object()
    movie = object()

    provider = SimpleNamespace(
        get_movie_by_number=lambda movie_number: calls.append(
            ("get", movie_number)
        ) or detail
    )
    importer = SimpleNamespace(
        upsert_movie_from_javdb_detail=lambda value, force_subscribed=False: calls.append(
            ("upsert", value, force_subscribed)
        ) or movie
    )
    monkeypatch.setattr(
        PluginContext,
        "build_javdb_provider",
        staticmethod(lambda: provider),
    )
    monkeypatch.setattr(
        PluginContext,
        "build_catalog_import_service",
        staticmethod(lambda: importer),
    )

    context = PluginContext(plugin_id="sample", settings={})
    result = context.import_movie_by_number("ABP-123", force_subscribed=True)

    assert result is movie
    assert calls == [
        ("get", "ABP-123"),
        ("upsert", detail, True),
    ]


def test_plugin_config_requires_explicit_valid_unique_ids():
    with pytest.raises(ValidationError, match="不允许包含重复插件 ID"):
        Plugins(enabled=["sample", "sample"])
    with pytest.raises(ValidationError, match="插件 ID 只能包含"):
        Plugins(enabled=["src.plugins.sample"])


def test_plugin_config_is_hidden_and_readonly_in_unified_config_api():
    assert "plugins" not in ConfigService._public_values()

    with pytest.raises(ApiError) as exc_info:
        ConfigService._reject_unknown_fields({"plugins": {"enabled": ["sample"]}})

    assert exc_info.value.code == "readonly_config_key"
