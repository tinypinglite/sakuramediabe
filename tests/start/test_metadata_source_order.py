from src.config.config import settings
from src.plugins import manager as manager_module
from src.plugins.manager import PluginManager


def test_reorder_metadata_sources_preserves_other_plugin_positions(monkeypatch):
    monkeypatch.setattr(
        settings,
        "plugins",
        settings.plugins.model_copy(deep=True),
    )
    monkeypatch.setattr(
        settings.plugins,
        "enabled",
        ["alpha", "other", "beta", "tail"],
    )
    monkeypatch.setattr(
        manager_module,
        "update_settings",
        lambda updated: setattr(settings, "plugins", updated.plugins),
    )

    PluginManager().reorder_enabled_metadata_sources(
        plugin_ids=["beta", "alpha"],
        metadata_plugin_ids={"alpha", "beta"},
    )

    assert settings.plugins.enabled == ["beta", "other", "alpha", "tail"]
