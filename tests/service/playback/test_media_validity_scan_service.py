import json

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.playback.media_validity_scan_service import MediaValidityScanService


class Reporter:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, dict]] = []

    def emit(self, *, current: int, total: int, summary_patch: dict) -> None:
        self.events.append((current, total, dict(summary_patch)))


def _media(
    library: MediaLibrary,
    number: str,
    *,
    storage_ref: dict | None = None,
    valid: bool = True,
) -> Media:
    movie = Movie.create(movie_number=number, javdb_id=f"javdb-{number}", title=number)
    return Media.create(
        movie=movie,
        library=library,
        storage_ref=storage_ref or {"path": f"{number}.mp4"},
        file_name=f"{number}.mp4",
        file_size_bytes=100,
        valid=valid,
    )


def _managed_ref_key(media_ref: dict) -> str:
    return json.dumps(media_ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_scan_reconciles_media_from_one_library_inventory(test_db, monkeypatch):
    library = MediaLibrary.create(name="validity-library", provider_key="demo", provider_config={})
    revived = _media(library, "VALIDITY-001", valid=False)
    invalidated = _media(library, "VALIDITY-002", valid=True)
    unchanged = _media(library, "VALIDITY-003", valid=True)
    calls: list[int] = []

    class Storage:
        def scan_managed_media_ref_keys(self):
            calls.append(1)
            return {_managed_ref_key(revived.storage_ref), _managed_ref_key(unchanged.storage_ref)}

        @staticmethod
        def managed_media_ref_key(*, media_ref):
            return _managed_ref_key(media_ref)

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage())
    reporter = Reporter()

    stats = MediaValidityScanService.scan_media_validity(reporter=reporter)

    assert stats == {
        "total_media": 3,
        "scanned_media": 3,
        "updated_media": 2,
        "unchanged_media": 1,
        "invalidated_media": 1,
        "revived_media": 1,
        "skipped_media": 0,
        "failed_media": 0,
        "scanned_libraries": 1,
        "unsupported_libraries": 0,
        "failed_libraries": 0,
    }
    assert calls == [1]
    assert Media.get_by_id(revived.id).valid is True
    assert Media.get_by_id(invalidated.id).valid is False
    assert Media.get_by_id(unchanged.id).valid is True
    assert [(current, total) for current, total, _summary in reporter.events] == [
        (1, 3),
        (2, 3),
        (3, 3),
    ]


def test_scan_skips_only_a_library_when_its_provider_inventory_fails(test_db, monkeypatch):
    failed_library = MediaLibrary.create(name="failed-library", provider_key="failed", provider_config={})
    working_library = MediaLibrary.create(name="working-library", provider_key="working", provider_config={})
    failed_media = _media(failed_library, "VALIDITY-001", valid=True)
    invalidated = _media(working_library, "VALIDITY-002", valid=True)

    class Storage:
        def __init__(self, provider_key: str) -> None:
            self.provider_key = provider_key

        def scan_managed_media_ref_keys(self):
            if self.provider_key == "failed":
                raise ProviderOperationError(
                    provider_key="failed",
                    operation="scan_managed_media_ref_keys",
                    code="unavailable",
                    safe_message="provider unavailable",
                    retryable=True,
                )
            return set()

        @staticmethod
        def managed_media_ref_key(*, media_ref):
            return _managed_ref_key(media_ref)

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda handle: Storage(handle.provider_key),
    )

    stats = MediaValidityScanService.scan_media_validity(reporter=Reporter())

    assert stats["failed_libraries"] == 1
    assert stats["skipped_media"] == 1
    assert stats["invalidated_media"] == 1
    assert Media.get_by_id(failed_media.id).valid is True
    assert Media.get_by_id(invalidated.id).valid is False


def test_scan_skips_a_provider_without_managed_inventory_capability(test_db, monkeypatch):
    library = MediaLibrary.create(name="legacy-library", provider_key="legacy", provider_config={})
    media = _media(library, "VALIDITY-001")

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: object())

    stats = MediaValidityScanService.scan_media_validity(reporter=Reporter())

    assert stats["unsupported_libraries"] == 1
    assert stats["skipped_media"] == 1
    assert Media.get_by_id(media.id).valid is True


def test_scan_does_not_request_inventory_for_an_empty_library(test_db, monkeypatch):
    MediaLibrary.create(name="empty-library", provider_key="cloud115", provider_config={})
    calls: list[object] = []

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _library: calls.append(object()),
    )

    stats = MediaValidityScanService.scan_media_validity(reporter=Reporter())

    assert stats["total_media"] == 0
    assert stats["scanned_libraries"] == 0
    assert calls == []


def test_scan_uses_provider_stable_key_for_cloud115_media(test_db, monkeypatch):
    library = MediaLibrary.create(name="cloud115-library", provider_key="cloud115", provider_config={})
    media = _media(
        library,
        "VALIDITY-001",
        storage_ref={
            "version": 1,
            "kind": "cloud115_media",
            "fid": "old-fid",
            "parent_cid": "old-parent",
            "pickcode": "stable-pickcode",
            "name": "old-name.mp4",
            "size_bytes": 1,
            "sha1": "old-sha",
            "is_dir": False,
        },
    )

    class Storage:
        @staticmethod
        def scan_managed_media_ref_keys():
            return {"stable-pickcode"}

        @staticmethod
        def managed_media_ref_key(*, media_ref):
            return media_ref["pickcode"]

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage())

    stats = MediaValidityScanService.scan_media_validity(reporter=Reporter())

    assert stats["unchanged_media"] == 1
    assert Media.get_by_id(media.id).valid is True


def test_scan_revival_resets_thumbnail_terminal_state(test_db, monkeypatch):
    library = MediaLibrary.create(name="revival-library", provider_key="demo", provider_config={})
    media = _media(library, "VALIDITY-001", valid=False)
    Media.update(
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_attempt_count=3,
        thumbnail_deferred_count=2,
        thumbnail_last_error_code="thumbnail_artifact_empty",
        thumbnail_last_error="bad thumbnail",
    ).where(Media.id == media.id).execute()

    class Storage:
        @staticmethod
        def scan_managed_media_ref_keys():
            return {_managed_ref_key(media.storage_ref)}

        @staticmethod
        def managed_media_ref_key(*, media_ref):
            return _managed_ref_key(media_ref)

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage())

    MediaValidityScanService.scan_media_validity(reporter=Reporter())

    revived = Media.get_by_id(media.id)
    assert revived.valid is True
    assert revived.thumbnail_generation_state == Media.THUMBNAIL_STATE_PENDING
    assert revived.thumbnail_attempt_count == 0
    assert revived.thumbnail_deferred_count == 0
    assert revived.thumbnail_last_error_code is None
    assert revived.thumbnail_last_error is None


def test_scan_excludes_media_created_after_its_snapshot_started(test_db, monkeypatch):
    library = MediaLibrary.create(name="snapshot-library", provider_key="demo", provider_config={})
    existing = _media(library, "VALIDITY-001")
    created_ids: list[int] = []

    class Storage:
        def scan_managed_media_ref_keys(self):
            created = _media(library, "VALIDITY-002")
            created_ids.append(created.id)
            return {_managed_ref_key(existing.storage_ref)}

        @staticmethod
        def managed_media_ref_key(*, media_ref):
            return _managed_ref_key(media_ref)

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: Storage())

    stats = MediaValidityScanService.scan_media_validity(reporter=Reporter())

    assert stats["total_media"] == 1
    assert stats["scanned_media"] == 1
    assert Media.get_by_id(existing.id).valid is True
    assert Media.get_by_id(created_ids[0]).valid is True
