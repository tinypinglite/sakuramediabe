from types import SimpleNamespace

import pytest

from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.service.catalog.metadata_source_service import (
    MetadataSourceError,
    MetadataSourceFailure,
    MetadataSourceService,
)


def _install_sources(monkeypatch, plugin_ids):
    MetadataSourceService.sources = tuple(
        (plugin_id, plugin_id, {}, SimpleNamespace())
        for plugin_id in plugin_ids
    )
    monkeypatch.setattr(settings.plugins, "enabled", list(plugin_ids))


def test_fetch_uses_first_successful_source_and_stops(monkeypatch):
    _install_sources(monkeypatch, ["first", "second"])
    calls: list[str] = []

    def fake_load(_cls, plugin_id, movie_number):
        calls.append(plugin_id)
        return (
            SimpleNamespace(movie_number=movie_number, source_id="1", source_url="u"),
            {"plugin_id": plugin_id, "display_name": plugin_id},
            set(),
        )

    monkeypatch.setattr(
        MetadataSourceService, "_load_plugin", classmethod(fake_load)
    )

    with MetadataSourceService.fetch("ABP-001") as outcome:
        assert outcome.source["plugin_id"] == "first"

    assert calls == ["first"]


def test_fetch_continues_after_not_found_and_failure(monkeypatch):
    _install_sources(monkeypatch, ["missing", "broken", "ok"])

    def fake_load(_cls, plugin_id, movie_number):
        if plugin_id == "missing":
            raise MetadataNotFoundError("movie", movie_number)
        if plugin_id == "broken":
            raise MetadataSourceError(
                "broken",
                failures=(
                    MetadataSourceFailure(
                        plugin_id="broken",
                        source_name="Broken",
                        detail="boom",
                    ),
                ),
            )
        return (
            SimpleNamespace(movie_number=movie_number, source_id=None, source_url=None),
            {"plugin_id": plugin_id, "display_name": plugin_id},
            set(),
        )

    monkeypatch.setattr(
        MetadataSourceService, "_load_plugin", classmethod(fake_load)
    )

    with MetadataSourceService.fetch("ABP-001") as outcome:
        assert outcome.source["plugin_id"] == "ok"
        assert [item.plugin_id for item in outcome.source_errors] == ["broken"]


def test_fetch_aggregates_all_source_failures(monkeypatch):
    _install_sources(monkeypatch, ["one", "two"])

    def fake_load(_cls, plugin_id, _movie_number):
        raise MetadataSourceError(
            plugin_id,
            failures=(
                MetadataSourceFailure(
                    plugin_id=plugin_id,
                    source_name=plugin_id,
                    detail=f"{plugin_id} failed",
                ),
            ),
        )

    monkeypatch.setattr(
        MetadataSourceService, "_load_plugin", classmethod(fake_load)
    )

    with (
        pytest.raises(MetadataSourceError) as exc_info,
        MetadataSourceService.fetch("ABP-001"),
    ):
        pass

    assert [item.plugin_id for item in exc_info.value.failures] == ["one", "two"]


def test_fetch_reports_not_found_when_every_source_returns_none(monkeypatch):
    _install_sources(monkeypatch, ["one"])

    def fake_load(_cls, _plugin_id, movie_number):
        raise MetadataNotFoundError("movie", movie_number)

    monkeypatch.setattr(
        MetadataSourceService, "_load_plugin", classmethod(fake_load)
    )

    with (
        pytest.raises(MetadataNotFoundError),
        MetadataSourceService.fetch("ABP-001"),
    ):
        pass


def test_fetch_reports_unavailable_when_no_source_is_enabled(monkeypatch):
    _install_sources(monkeypatch, [])

    with (
        pytest.raises(MetadataSourceError, match="未启用任何元数据插件"),
        MetadataSourceService.fetch("ABP-001"),
    ):
        pass
