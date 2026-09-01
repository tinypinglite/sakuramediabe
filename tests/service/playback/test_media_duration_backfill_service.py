from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.playback.media_duration_backfill_service import (
    MediaDurationBackfillService,
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
    duration_seconds: int = 0,
    valid: bool = True,
) -> Media:
    movie = Movie.create(movie_number=number, javdb_id=f"javdb-{number}", title=number)
    return Media.create(
        movie=movie,
        library=library,
        storage_ref={"path": f"{number}.mp4"},
        file_name=f"{number}.mp4",
        file_size_bytes=100,
        duration_seconds=duration_seconds,
        valid=valid,
    )


def test_backfill_calculates_only_missing_valid_durations(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="duration-library", provider_key="demo", provider_config={}
    )
    first = _media(library, "DURATION-001")
    second = _media(library, "DURATION-002")
    existing = _media(library, "DURATION-003", duration_seconds=30)
    invalid = _media(library, "DURATION-004", valid=False)
    probed_ids: list[int] = []

    class Storage:
        def probe_duration_seconds(self, *, media):
            probed_ids.append(media.media_id)
            return media.media_id + 100

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )
    reporter = Reporter()

    stats = MediaDurationBackfillService.backfill_missing_durations(reporter=reporter)

    assert stats == {
        "missing_media": 2,
        "updated_media": 2,
        "failed_media": 0,
        "skipped_media": 0,
    }
    assert probed_ids == [first.id, second.id]
    assert Media.get_by_id(first.id).duration_seconds == first.id + 100
    assert Media.get_by_id(second.id).duration_seconds == second.id + 100
    assert Media.get_by_id(existing.id).duration_seconds == 30
    assert Media.get_by_id(invalid.id).duration_seconds == 0
    assert [(current, total) for current, total, _summary in reporter.events] == [
        (1, 2),
        (2, 2),
    ]


def test_backfill_continues_after_a_provider_failure(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="duration-library", provider_key="demo", provider_config={}
    )
    failed_media = _media(library, "DURATION-001")
    updated_media = _media(library, "DURATION-002")

    class Storage:
        def probe_duration_seconds(self, *, media):
            if media.media_id == failed_media.id:
                raise ProviderOperationError(
                    provider_key="demo",
                    operation="probe_duration_seconds",
                    code="unavailable",
                    safe_message="provider unavailable",
                    retryable=True,
                )
            return 120

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )

    stats = MediaDurationBackfillService.backfill_missing_durations(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 2,
        "updated_media": 1,
        "failed_media": 1,
        "skipped_media": 0,
    }
    assert Media.get_by_id(failed_media.id).duration_seconds == 0
    assert Media.get_by_id(updated_media.id).duration_seconds == 120


def test_backfill_skips_a_provider_without_duration_probe(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="duration-library", provider_key="legacy", provider_config={}
    )
    media = _media(library, "DURATION-001")

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: object()
    )

    stats = MediaDurationBackfillService.backfill_missing_durations(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 1,
        "updated_media": 0,
        "failed_media": 0,
        "skipped_media": 1,
    }
    assert Media.get_by_id(media.id).duration_seconds == 0


def test_backfill_does_not_overwrite_duration_changed_during_probe(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="duration-library", provider_key="demo", provider_config={}
    )
    media = _media(library, "DURATION-001")

    class Storage:
        def probe_duration_seconds(self, *, media):
            Media.update(duration_seconds=240).where(Media.id == media.media_id).execute()
            return 120

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )

    stats = MediaDurationBackfillService.backfill_missing_durations(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 1,
        "updated_media": 0,
        "failed_media": 0,
        "skipped_media": 1,
    }
    assert Media.get_by_id(media.id).duration_seconds == 240
