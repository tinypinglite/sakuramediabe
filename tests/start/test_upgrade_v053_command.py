from types import SimpleNamespace

from click.testing import CliRunner

from src.start.commands import main


def _summary(*, upgraded: bool):
    return SimpleNamespace(upgraded=upgraded, media_count=3, invalid_media_count=1)


def test_upgrade_v053_command_is_noop_without_legacy_database(monkeypatch):
    database = object()
    monkeypatch.setattr(
        "src.start.commands._connect_database_for_migration", lambda: database
    )
    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.classify_database_schema",
        lambda value: "current" if value is database else "unexpected",
    )
    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.upgrade_v053_database",
        lambda value, *, dry_run: _summary(upgraded=False),
    )
    monkeypatch.setattr(
        "src.plugins.bundled_providers.install_bundled_provider_plugins_once",
        lambda: (_ for _ in ()).throw(AssertionError("must not install")),
    )

    result = CliRunner().invoke(main, ["upgrade-v053"])

    assert result.exit_code == 0, result.output
    assert "upgraded=false media=3 invalid_media=1" in result.output


def test_upgrade_v053_command_installs_and_loads_both_providers_before_bridge(
    monkeypatch,
):
    database = object()
    events: list[str] = []
    monkeypatch.setattr(
        "src.start.commands._connect_database_for_migration", lambda: database
    )
    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.classify_database_schema",
        lambda value: "legacy_v053" if value is database else "unexpected",
    )
    monkeypatch.setattr(
        "src.plugins.bundled_providers.install_bundled_provider_plugins_once",
        lambda: (
            events.append("install")
            or SimpleNamespace(installed=True, already_completed=False)
        ),
    )
    monkeypatch.setattr(
        "src.plugins.loader.load_enabled_plugins",
        lambda *_args, **_kwargs: events.append("load") or (),
    )
    monkeypatch.setattr(
        "src.plugins.provider_protocol.MEDIA_PROVIDER_REGISTRY.require",
        lambda provider_key: events.append(f"require:{provider_key}"),
    )

    def upgrade(value, *, dry_run: bool):
        assert value is database
        assert dry_run is False
        events.append("upgrade")
        return _summary(upgraded=True)

    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.upgrade_v053_database", upgrade
    )
    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.cleanup_legacy_v053_qdrant_collections",
        lambda: events.append("cleanup-qdrant"),
    )

    result = CliRunner().invoke(main, ["upgrade-v053"])

    assert result.exit_code == 0, result.output
    assert events == [
        "install",
        "load",
        "require:local",
        "require:cloud115",
        "upgrade",
        "cleanup-qdrant",
    ]


def test_upgrade_v053_command_supports_read_only_preflight(monkeypatch):
    database = object()
    monkeypatch.setattr(
        "src.start.commands._connect_database_for_migration", lambda: database
    )
    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.classify_database_schema",
        lambda _value: "current",
    )

    def upgrade(value, *, dry_run: bool):
        assert value is database
        assert dry_run is True
        return _summary(upgraded=False)

    monkeypatch.setattr(
        "src.start.legacy_v053_upgrade.upgrade_v053_database", upgrade
    )

    result = CliRunner().invoke(main, ["upgrade-v053", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "dry_run=true upgraded=false media=3 invalid_media=1" in result.output
