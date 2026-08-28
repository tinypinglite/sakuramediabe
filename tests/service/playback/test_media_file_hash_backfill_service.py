from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.playback.media_file_hash_backfill_service import (
    MediaFileHashBackfillService,
)


class Reporter:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, dict]] = []

    def emit(self, *, current: int, total: int, summary_patch: dict) -> None:
        self.events.append((current, total, dict(summary_patch)))


def _media(library: MediaLibrary, number: str, *, file_hash: str | None) -> Media:
    movie = Movie.create(movie_number=number, javdb_id=f"javdb-{number}", title=number)
    return Media.create(
        movie=movie,
        library=library,
        storage_ref={"path": f"{number}.mp4"},
        file_name=f"{number}.mp4",
        file_size_bytes=100,
        file_hash=file_hash,
    )


def test_backfill_calculates_only_missing_file_hashes(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="hash-library", provider_key="demo", provider_config={}
    )
    null_hash = _media(library, "HASH-001", file_hash=None)
    empty_hash = _media(library, "HASH-002", file_hash="")
    existing_hash = _media(library, "HASH-003", file_hash="already-present")
    computed_ids: list[int] = []

    class Storage:
        def compute_file_hash(self, *, media):
            computed_ids.append(media.media_id)
            return f"hash-{media.media_id}"

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )
    reporter = Reporter()

    stats = MediaFileHashBackfillService.backfill_missing_file_hashes(reporter=reporter)

    assert stats == {
        "missing_media": 2,
        "updated_media": 2,
        "failed_media": 0,
        "skipped_media": 0,
    }
    assert computed_ids == [null_hash.id, empty_hash.id]
    assert Media.get_by_id(null_hash.id).file_hash == f"hash-{null_hash.id}"
    assert Media.get_by_id(empty_hash.id).file_hash == f"hash-{empty_hash.id}"
    assert Media.get_by_id(existing_hash.id).file_hash == "already-present"
    assert [(current, total) for current, total, _summary in reporter.events] == [
        (1, 2),
        (2, 2),
    ]


def test_backfill_continues_after_a_provider_failure(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="hash-library", provider_key="demo", provider_config={}
    )
    failed_media = _media(library, "HASH-001", file_hash=None)
    updated_media = _media(library, "HASH-002", file_hash=None)

    class Storage:
        def compute_file_hash(self, *, media):
            if media.media_id == failed_media.id:
                raise ProviderOperationError(
                    provider_key="demo",
                    operation="compute_file_hash",
                    code="unavailable",
                    safe_message="provider unavailable",
                    retryable=True,
                )
            return "calculated-hash"

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage()
    )

    stats = MediaFileHashBackfillService.backfill_missing_file_hashes(
        reporter=Reporter()
    )

    assert stats == {
        "missing_media": 2,
        "updated_media": 1,
        "failed_media": 1,
        "skipped_media": 0,
    }
    assert Media.get_by_id(failed_media.id).file_hash is None
    assert Media.get_by_id(updated_media.id).file_hash == "calculated-hash"
