from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import ImportFile, StagedMedia
from src.service.transfers.imports.import_service import MediaImportService


def test_retry_uses_persisted_file_ref_and_associates_media(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="retry-storage-library", provider_key="test", provider_config={}
    )
    movie = Movie.create(movie_number="ABC-001", title="selected movie")
    staged = StagedMedia(
        storage_ref={"stored": "media"},
        receipt={"receipt": "retry"},
        size_bytes=100,
        duration_seconds=90,
        video_info=None,
    )
    events = []
    stored_source = []
    finalize_observed = []

    class Storage:
        def stage_import_file(self, *, source, placement, source_disposition, operation_key):
            stored_source.append((source, placement.relative_path, operation_key))
            events.append("stage")
            return staged

        def compute_file_hash(self, *, media):
            events.append("write")
            return "f" * 64

        def finalize_import(self, *, receipt):
            assert receipt == staged.receipt
            events.append("finalize")
            finalize_observed.append(Movie.get_by_id(movie.id).is_subscribed)

    monkeypatch.setattr(
        "src.service.catalog.movie_metadata_search_service.MovieMetadataSearchService.fetch_candidate",
        lambda _candidate_id: nullcontext(
            (SimpleNamespace(movie_number="ABC-001"), {"plugin_id": "demo"})
        ),
    )
    captured_force_subscribed = []

    def import_movie(_detail, _source, force_subscribed=False):
        captured_force_subscribed.append(force_subscribed)
        return movie, False

    catalog = SimpleNamespace(import_movie=import_movie)
    failure_item = {
        "source_ref": {"file": "opaque-file"},
        "name": "ABC-001.mp4",
        "relative_path": "release/ABC-001.mp4",
        "size_bytes": 100,
        "is_video": True,
        "media_kind": "jav",
        "library_id": library.id,
        "source_disposition": "keep",
    }

    result = MediaImportService(
        provider=Storage(), catalog_import_service=catalog
    ).retry_failed_file(
        failure_item,
        "plugin:demo:ABC-001",
        operation_key="retry-operation",
    )

    assert result["movie_id"] == movie.id
    assert result["media_id"] == Media.select().get().id
    assert events == ["stage", "write", "finalize"]
    source, placement, operation_key = stored_source[0]
    assert source == ImportFile(
        source_ref={"file": "opaque-file"},
        name="ABC-001.mp4",
        relative_path="release/ABC-001.mp4",
        size_bytes=100,
        is_video=True,
    )
    assert placement == "jav/ABC-001/ABC-001.mp4"
    assert operation_key == "retry-operation"
    # 重试不再在 catalog import 阶段强制订阅，而是在 finalize 成功后订阅。
    assert captured_force_subscribed == [False]
    assert finalize_observed == [False]
    stored_movie = Movie.get_by_id(movie.id)
    assert stored_movie.is_subscribed is True
    assert stored_movie.subscribed_at is not None


def test_retry_in_place_requires_provider_capability(test_db):
    library = MediaLibrary.create(
        name="retry-in-place-library", provider_key="test", provider_config={}
    )

    class Storage:
        pass

    failure_item = {
        "source_ref": {"file": "opaque-file"},
        "name": "ABC-001.mp4",
        "relative_path": "release/ABC-001.mp4",
        "size_bytes": 100,
        "is_video": True,
        "media_kind": "jav",
        "library_id": library.id,
        "source_disposition": "in_place",
    }

    with pytest.raises(ApiError) as exc:
        MediaImportService(provider=Storage(), catalog_import_service=object()).retry_failed_file(
            failure_item,
            "plugin:demo:ABC-001",
            operation_key="retry-in-place",
        )

    assert exc.value.code == "in_place_import_unsupported"
