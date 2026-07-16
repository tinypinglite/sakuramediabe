import json

import pytest
import toml
from pydantic import ValidationError

import src.config.config as config_module
from src.api.exception.errors import ApiError
from src.config.config import Settings
from src.schema.system.config import ConfigEffectLevel
from src.service.system.config_service import ConfigService, resolve_effect


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    # 快照当前运行时配置，重定向落盘目标到临时文件，用例结束后还原全局单例。
    original_runtime_settings = Settings.model_validate(config_module.settings.model_dump())
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(json.loads(original_runtime_settings.model_dump_json())),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    yield config_path

    config_module.refresh_runtime_settings(original_runtime_settings)


def _persisted(config_path) -> dict:
    return toml.load(config_path)


def test_get_config_returns_all_sections_and_effects(isolated_config):
    resource = ConfigService.get_config()

    # values 覆盖除只读键（auth / enable_docs）外的全部配置节；敏感字段明文返回（前端自律）。
    for section in ("database", "media", "metadata", "scheduler", "qdrant"):
        assert section in resource.values
    # 只读键既不在 values（避免 auth 密钥外流 / enable_docs 混入前端可改集合），也不在 effects。
    assert "auth" not in resource.values
    assert "auth" not in resource.effects
    assert "enable_docs" not in resource.values
    assert "enable_docs" not in resource.effects
    # effects 给出节级生效方式。
    assert resource.effects["metadata"] == ConfigEffectLevel.HOT
    assert resource.effects["scheduler"] == ConfigEffectLevel.RESTART_SCHEDULER
    assert resource.effects["database"] == ConfigEffectLevel.RESTART_API


def test_update_hot_field_marks_applied_and_persists(isolated_config):
    result = ConfigService.update_config({"metadata": {"proxy": "socks5://127.0.0.1:1080"}})

    assert result.applied == ["metadata.proxy"]
    assert result.pending_restart == []
    # 内存运行时与磁盘 toml 同步更新。
    assert config_module.settings.metadata.proxy == "socks5://127.0.0.1:1080"
    assert _persisted(isolated_config)["metadata"]["proxy"] == "socks5://127.0.0.1:1080"
    assert result.values["metadata"]["proxy"] == "socks5://127.0.0.1:1080"


def test_update_cron_field_requires_scheduler_restart(isolated_config):
    result = ConfigService.update_config({"scheduler": {"movie_heat_cron": "0 6 * * *"}})

    assert result.applied == []
    assert len(result.pending_restart) == 1
    assert result.pending_restart[0].field == "scheduler.movie_heat_cron"
    assert result.pending_restart[0].restart == "scheduler"
    assert config_module.settings.scheduler.movie_heat_cron == "0 6 * * *"
    assert _persisted(isolated_config)["scheduler"]["movie_heat_cron"] == "0 6 * * *"


def test_update_rejects_readonly_enable_docs(isolated_config):
    # enable_docs 属只读键：字段仍在 Settings 里（默认关闭），但改动需走 toml/env + 重启。
    original = config_module.settings.enable_docs
    original_persisted = _persisted(isolated_config).get("enable_docs")

    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({"enable_docs": True})
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "readonly_config_key"
    assert exc_info.value.details == {"field": "enable_docs"}
    # 内存和磁盘均未被改动。
    assert config_module.settings.enable_docs == original
    assert _persisted(isolated_config).get("enable_docs") == original_persisted


def test_update_multiple_sections_classifies_each_field(isolated_config):
    result = ConfigService.update_config(
        {
            "metadata": {"proxy": "http://127.0.0.1:7890"},
            "downloads": {"small_file_cleanup_threshold_mb": 128},
        }
    )

    assert result.applied == ["metadata.proxy"]
    pending = {(p.field, p.restart) for p in result.pending_restart}
    assert pending == {("downloads.small_file_cleanup_threshold_mb", "scheduler")}


def test_update_rejects_empty_payload(isolated_config):
    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({})
    assert exc_info.value.code == "empty_config_update"


def test_update_rejects_unknown_section(isolated_config):
    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({"not_a_section": {"x": 1}})
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "unknown_config_field"
    assert exc_info.value.details == {"field": "not_a_section"}


def test_update_rejects_unknown_sub_field(isolated_config):
    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({"metadata": {"nope": 1}})
    assert exc_info.value.code == "unknown_config_field"
    assert exc_info.value.details == {"field": "metadata.nope"}


def test_update_rejects_section_that_is_not_object(isolated_config):
    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({"metadata": "oops"})
    assert exc_info.value.code == "invalid_config_value"


def test_update_rejects_invalid_cron(isolated_config):
    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({"scheduler": {"movie_heat_cron": "not a cron"}})
    assert exc_info.value.code == "invalid_config_value"
    # 非法值不得落盘。
    assert _persisted(isolated_config)["scheduler"]["movie_heat_cron"] != "not a cron"


def test_update_rejects_invalid_url(isolated_config):
    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({"qdrant": {"url": "localhost:6333"}})
    assert exc_info.value.code == "invalid_config_value"


def test_update_rejects_readonly_auth(isolated_config):
    with pytest.raises(ApiError) as exc_info:
        ConfigService.update_config({"auth": {"username": "foo"}})
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "readonly_config_key"
    assert exc_info.value.details == {"field": "auth"}


def test_resolve_effect_falls_back_to_section_default():
    assert resolve_effect("scheduler.movie_heat_cron") == ConfigEffectLevel.RESTART_SCHEDULER
    assert resolve_effect("metadata.proxy") == ConfigEffectLevel.HOT
    # 未登记的顶层名保守回落到 restart_api，避免"改了以为生效"的假象。
    assert resolve_effect("unknown_section.foo") == ConfigEffectLevel.RESTART_API


def test_download_config_effect_levels_match_runtime_consumers():
    assert (
        resolve_effect("downloads.cloud115_progress_poll_interval_seconds")
        == ConfigEffectLevel.HOT
    )
    assert (
        resolve_effect("downloads.progress_stream_poll_interval_seconds")
        == ConfigEffectLevel.RESTART_API
    )
    for field in (
        "cloud115_offline_abandon_hours",
        "small_file_cleanup_threshold_mb",
        "preferred_client_kinds",
    ):
        assert resolve_effect(f"downloads.{field}") == ConfigEffectLevel.RESTART_SCHEDULER


def test_cloud115_offline_abandon_hours_minimum_is_one():
    with pytest.raises(ValidationError):
        Settings.model_validate({"downloads": {"cloud115_offline_abandon_hours": 0}})
