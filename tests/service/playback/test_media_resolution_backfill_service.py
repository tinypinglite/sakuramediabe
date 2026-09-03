from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.playback.media_resolution_backfill_service import (
    MediaResolutionBackfillService,
)


class Reporter:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, dict]] = []

    def emit(self, *, current: int, total: int, summary_patch: dict) -> None:
        self.events.append((current, total, dict(summary_patch)))


def _media(
    library: MediaLibrary,
    number: str,
    *,
    resolution: str | None = None,
    valid: bool = True,
) -> Media:
    movie = Movie.create(movie_number=number, javdb_id=f"javdb-{number}", title=number)
    return Media.create(
        movie=movie,
        library=library,
        storage_ref={"path": f"{number}.mp4"},
        file_name=f"{number}.mp4",
        file_size_bytes=100,
        resolution=resolution,
        valid=valid,
    )


def test_backfill_calculates_only_missing_valid_resolutions(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="resolution-library", provider_key="demo", provider_config={}
    )
    first = _media(library, "RESOLUTION-001")
    second = _media(library, "RESOLUTION-002", resolution="   ")
    existing = _media(library, "RESOLUTION-003", resolution="1920x1080")
    invalid = _media(library, "RESOLUTION-004", valid=False)
    probed_ids: list[int] = []

    class Storage:
        def probe_resolution(self, *, media):
            probed_ids.append(media.media_id)
            return "720X1280" if media.media_id == first.id else "1080x1920"

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )
    reporter = Reporter()

    stats = MediaResolutionBackfillService.backfill_missing_resolutions(
        reporter=reporter
    )

    assert stats == {
        "missing_media": 2,
        "updated_media": 2,
        "failed_media": 0,
        "skipped_media": 0,
    }
    assert probed_ids == [first.id, second.id]
    assert Media.get_by_id(first.id).resolution == "720x1280"
    assert Media.get_by_id(second.id).resolution == "1080x1920"
    assert Media.get_by_id(existing.id).resolution == "1920x1080"
    assert Media.get_by_id(invalid.id).resolution is None
    assert [(current, total) for current, total, _summary in reporter.events] == [
        (1, 2),
        (2, 2),
    ]


def test_backfill_continues_after_invalid_provider_resolution(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="resolution-library", provider_key="demo", provider_config={}
    )
    failed_media = _media(library, "RESOLUTION-001")
    updated_media = _media(library, "RESOLUTION-002")

    class Storage:
        def probe_resolution(self, *, media):
            if media.media_id == failed_media.id:
                return "not-a-resolution"
            return "720x1280"

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )

    stats = MediaResolutionBackfillService.backfill_missing_resolutions(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 2,
        "updated_media": 1,
        "failed_media": 1,
        "skipped_media": 0,
    }
    assert Media.get_by_id(failed_media.id).resolution is None
    assert Media.get_by_id(updated_media.id).resolution == "720x1280"


def test_backfill_skips_provider_without_resolution_probe(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="resolution-library", provider_key="legacy", provider_config={}
    )
    media = _media(library, "RESOLUTION-001")

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: object()
    )

    stats = MediaResolutionBackfillService.backfill_missing_resolutions(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 1,
        "updated_media": 0,
        "failed_media": 0,
        "skipped_media": 1,
    }
    assert Media.get_by_id(media.id).resolution is None


def test_backfill_skips_media_when_provider_cannot_observe_resolution(
    test_db, monkeypatch
):
    library = MediaLibrary.create(
        name="resolution-library", provider_key="demo", provider_config={}
    )
    media = _media(library, "RESOLUTION-001")

    class Storage:
        def probe_resolution(self, *, media):
            return None

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )

    stats = MediaResolutionBackfillService.backfill_missing_resolutions(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 1,
        "updated_media": 0,
        "failed_media": 0,
        "skipped_media": 1,
    }
    assert Media.get_by_id(media.id).resolution is None


def test_backfill_does_not_overwrite_resolution_changed_during_probe(
    test_db, monkeypatch
):
    library = MediaLibrary.create(
        name="resolution-library", provider_key="demo", provider_config={}
    )
    media = _media(library, "RESOLUTION-001")

    class Storage:
        def probe_resolution(self, *, media):
            Media.update(resolution="3840x2160").where(
                Media.id == media.media_id
            ).execute()
            return "720x1280"

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )

    stats = MediaResolutionBackfillService.backfill_missing_resolutions(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 1,
        "updated_media": 0,
        "failed_media": 0,
        "skipped_media": 1,
    }
    assert Media.get_by_id(media.id).resolution == "3840x2160"
