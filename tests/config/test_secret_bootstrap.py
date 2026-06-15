import toml
import pytest

import src.config.config as config_module
from src.config.config import Settings, ensure_runtime_config


@pytest.fixture
def bootstrap_env(tmp_path, monkeypatch):
    """把持久化目标指向临时文件，并隔离全局 settings.auth，避免污染其它用例。"""
    target = tmp_path / "config.toml"
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", target)
    monkeypatch.setattr(config_module.settings.auth, "secret_key", "")
    monkeypatch.setattr(config_module.settings.auth, "file_signature_secret", "")
    return target


def test_writes_full_default_config_when_file_missing(bootstrap_env):
    target = bootstrap_env
    assert not target.exists()

    changed = ensure_runtime_config()

    assert changed is True
    assert target.exists()
    persisted = toml.load(target)
    # 全部配置项都应写入，而不仅仅是 [auth]。
    for section in ("database", "auth", "media", "metadata", "scheduler", "image_search", "qdrant"):
        assert section in persisted, f"missing section: {section}"
    # 默认值正确落盘。
    assert persisted["database"]["engine"] == "sqlite"
    assert persisted["scheduler"]["actor_subscription_sync_cron"] == "0 2 * * *"
    # 生成的密钥已写入且与内存一致。
    assert persisted["auth"]["secret_key"] == config_module.settings.auth.secret_key
    assert persisted["auth"]["file_signature_secret"] == config_module.settings.auth.file_signature_secret
    assert persisted["auth"]["secret_key"]
    # 落盘内容可被 Settings 原样回读。
    assert Settings().database.engine.value == "sqlite"


def test_writes_full_default_config_when_file_empty(bootstrap_env):
    target = bootstrap_env
    target.write_text("   \n", encoding="utf-8")

    changed = ensure_runtime_config()

    assert changed is True
    persisted = toml.load(target)
    assert "database" in persisted and "scheduler" in persisted
    assert persisted["auth"]["secret_key"]


@pytest.mark.parametrize("placeholder", ["replace-with-a-random-secret-key", "98765432178965437"])
def test_surgical_patch_regenerates_insecure_secret_on_existing_file(bootstrap_env, placeholder):
    target = bootstrap_env
    # 文件已有内容（含模型外字段），走 surgical 路径：只补 [auth] 密钥，保留其余配置。
    target.write_text(
        toml.dumps(
            {
                "auth": {"username": "account", "secret_key": placeholder},
                "scheduler": {"timezone": "Asia/Shanghai"},
                "indexer_settings": {"indexers": [{"name": "mteam", "kind": "pt"}]},
            }
        ),
        encoding="utf-8",
    )
    config_module.settings.auth.secret_key = placeholder

    changed = ensure_runtime_config()

    assert changed is True
    persisted = toml.load(target)
    # 模型外字段未被破坏。
    assert persisted["scheduler"]["timezone"] == "Asia/Shanghai"
    assert persisted["indexer_settings"]["indexers"][0]["name"] == "mteam"
    # 占位密钥被替换为非空随机值。
    assert persisted["auth"]["secret_key"] != placeholder
    assert persisted["auth"]["secret_key"]
    assert persisted["auth"]["username"] == "account"


def test_idempotent_when_existing_file_has_valid_secrets(bootstrap_env):
    target = bootstrap_env
    target.write_text(
        toml.dumps(
            {
                "auth": {
                    "secret_key": "a-valid-random-secret-value",
                    "file_signature_secret": "another-valid-secret",
                },
                "scheduler": {"timezone": "Asia/Shanghai"},
            }
        ),
        encoding="utf-8",
    )
    config_module.settings.auth.secret_key = "a-valid-random-secret-value"
    config_module.settings.auth.file_signature_secret = "another-valid-secret"
    before = target.read_text(encoding="utf-8")

    changed = ensure_runtime_config()

    assert changed is False
    # 无变更则不改动文件内容。
    assert target.read_text(encoding="utf-8") == before
