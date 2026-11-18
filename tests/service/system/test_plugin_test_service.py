from datetime import date

import pytest
from PIL import Image as PillowImage

from src.config.config import settings
from src.plugins import (
    METADATA_SOURCE_EXTENSION_KEY,
    REVIEWS_EXTENSION_KEY,
    STATS_SOURCE_EXTENSION_KEY,
    PluginExtension,
    PluginMetadataSource,
    PluginMovieMetadata,
    PluginMovieReview,
    PluginMovieReviewSource,
    PluginMovieStatsField,
    PluginMovieStatsSnapshot,
    PluginMovieStatsSource,
    PluginRegistration,
)
from src.service.catalog.metadata_source_service import MetadataSourceService
from src.service.system.plugin_test_service import (
    PluginTestCapabilityUnsupported,
    PluginTestService,
)


@pytest.fixture(autouse=True)
def isolated_plugin_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "plugins", settings.plugins.model_copy(deep=True))
    monkeypatch.setattr(settings.plugins, "root_dir", str(tmp_path / "plugins"))
    monkeypatch.setattr(settings.plugins, "enabled", [])
    yield


def _registration(
    plugin_id: str,
    extension: PluginExtension,
    *,
    test_movie_number: str | None = None,
) -> PluginRegistration:
    return PluginRegistration(
        plugin_id=plugin_id,
        display_name=plugin_id,
        version="1.0.0",
        test_movie_number=test_movie_number,
        extensions=(extension,),
    )


def test_describe_uses_plugin_test_number_and_capabilities(monkeypatch):
    registration = _registration(
        "reviews_plugin",
        PluginExtension(
            key=REVIEWS_EXTENSION_KEY,
            data=PluginMovieReviewSource(
                fetch_reviews=lambda *_args: [],
            ),
        ),
        test_movie_number="FC2-PPV-4972378",
    )
    monkeypatch.setattr(PluginTestService, "registrations", (registration,))

    assert PluginTestService.describe("reviews_plugin") == (
        ("reviews",),
        "FC2-PPV-4972378",
    )
    assert PluginTestService.describe("missing_plugin") == ((), None)


def test_metadata_test_returns_summary_without_importing(
    tmp_path, monkeypatch
):
    plugin_id = "metadata_plugin"
    root = tmp_path / "plugins"
    request_dir = root / plugin_id / "data" / "metadata-tmp" / "test"
    request_dir.mkdir(parents=True)
    cover = request_dir / "cover.jpg"
    plot = request_dir / "plot.jpg"
    for path in (cover, plot):
        PillowImage.new("RGB", (2, 2), "white").save(path)
    monkeypatch.setattr(settings.plugins, "root_dir", str(root))
    monkeypatch.setattr(settings.plugins, "enabled", [plugin_id])

    metadata = PluginMovieMetadata(
        movie_number="SSIS-001",
        title="测试影片",
        release_date=date(2024, 1, 1),
        duration_minutes=120,
        cover_image_path=str(cover),
        plot_image_paths=[str(plot)],
        source_url="https://example.invalid/SSIS-001",
    )
    registration = _registration(
        plugin_id,
        PluginExtension(
            key=METADATA_SOURCE_EXTENSION_KEY,
            data=PluginMetadataSource(fetch_movie=lambda _number: metadata),
        ),
        test_movie_number="SSIS-001",
    )
    monkeypatch.setattr(PluginTestService, "registrations", (registration,))
    monkeypatch.setattr(MetadataSourceService, "sources", ())
    MetadataSourceService.register((registration,))

    result = PluginTestService.run(plugin_id, "metadata")

    assert result.status == "ok"
    assert result.movie_number == "SSIS-001"
    assert result.metadata is not None
    assert result.metadata.title == "测试影片"
    assert result.metadata.image_count == 2
    assert not cover.exists()
    assert not plot.exists()


def test_stats_test_validates_snapshot_without_writing_movie(
    tmp_path, monkeypatch
):
    registration = _registration(
        "stats_plugin",
        PluginExtension(
            key=STATS_SOURCE_EXTENSION_KEY,
            data=PluginMovieStatsSource(
                fields=[
                    PluginMovieStatsField(
                        key="score",
                        value_type="float",
                        max=5,
                    )
                ],
                heat_version="v1",
                fetch_stats=lambda _number: PluginMovieStatsSnapshot(
                    values={"score": 4.5},
                    heat=1234,
                ),
            ),
        ),
        test_movie_number="SSIS-001",
    )
    monkeypatch.setattr(PluginTestService, "registrations", (registration,))

    result = PluginTestService.run("stats_plugin", "stats")

    assert result.status == "ok"
    assert result.stats is not None
    assert result.stats.heat == 1234
    assert result.stats.values == {"score": 4.5}


def test_reviews_test_returns_first_page_preview(monkeypatch):
    registration = _registration(
        "reviews_plugin",
        PluginExtension(
            key=REVIEWS_EXTENSION_KEY,
            data=PluginMovieReviewSource(
                fetch_reviews=lambda _number, _page, _size, _sort: [
                    PluginMovieReview(
                        id=1,
                        content="第一条评论",
                        username="tester",
                    )
                ],
            ),
        ),
        test_movie_number="SSIS-001",
    )
    monkeypatch.setattr(PluginTestService, "registrations", (registration,))

    result = PluginTestService.run("reviews_plugin", "reviews")

    assert result.status == "ok"
    assert result.reviews is not None
    assert result.reviews.item_count == 1
    assert result.reviews.first_username == "tester"
    assert result.reviews.first_excerpt == "第一条评论"


def test_failed_plugin_call_is_returned_as_failed_result(monkeypatch):
    registration = _registration(
        "reviews_plugin",
        PluginExtension(
            key=REVIEWS_EXTENSION_KEY,
            data=PluginMovieReviewSource(
                fetch_reviews=lambda *_args: (_ for _ in ()).throw(
                    RuntimeError("upstream unavailable")
                ),
            ),
        ),
        test_movie_number="SSIS-001",
    )
    monkeypatch.setattr(PluginTestService, "registrations", (registration,))

    result = PluginTestService.run("reviews_plugin", "reviews")

    assert result.status == "failed"
    assert result.error_type == "RuntimeError"
    assert result.error_message == "upstream unavailable"


def test_unsupported_capability_raises(monkeypatch):
    registration = _registration(
        "reviews_plugin",
        PluginExtension(
            key=REVIEWS_EXTENSION_KEY,
            data=PluginMovieReviewSource(fetch_reviews=lambda *_args: []),
        ),
    )
    monkeypatch.setattr(PluginTestService, "registrations", (registration,))

    with pytest.raises(PluginTestCapabilityUnsupported):
        PluginTestService.run("reviews_plugin", "stats")
