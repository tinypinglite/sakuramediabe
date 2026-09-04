from contextlib import contextmanager
from threading import Event, Thread
from types import SimpleNamespace

import pytest

from src.model import Media, MediaLibrary, VideoItem, get_database
from src.plugins.manager import PluginManager
from src.plugins.operation_lock import plugin_operation_lock
from src.service.playback.media_clip_service import MediaClipService
from src.service.playback.media_library_service import MediaLibraryService
from src.service.playback.media_service import MediaService
from src.service.playback.media_validity_scan_service import MediaValidityScanService
from src.service.playback.operation_locks import (
    LIBRARY_LOCK,
    MEDIA_LOCK,
    MediaOperationBusy,
    media_operation_lock,
)
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService


@contextmanager
def other_session_lock(namespace, resource_id):
    ready, release = Event(), Event()
    failures = []

    def hold():
        try:
            with (
                get_database().connection_context(),
                media_operation_lock(namespace, resource_id),
            ):
                ready.set()
                assert release.wait(10)
        except BaseException as error:
            failures.append(error)
            ready.set()

    thread = Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(10)
        assert not failures
        yield
    finally:
        release.set()
        thread.join(10)
        assert not thread.is_alive() and not failures


def test_media_lock_releases_after_exception_and_blocks_other_session(test_db):
    with (
        other_session_lock(MEDIA_LOCK, 123),
        pytest.raises(MediaOperationBusy),
        media_operation_lock(MEDIA_LOCK, 123),
    ):
        pytest.fail("must not enter")
    with pytest.raises(ValueError), media_operation_lock(MEDIA_LOCK, 123):
        raise ValueError("injected")
    with other_session_lock(MEDIA_LOCK, 123):
        pass


def test_connection_loss_invalidates_owner_and_releases_lock(test_db):
    with media_operation_lock(MEDIA_LOCK, 123) as check:
        get_database().close()
        with pytest.raises(RuntimeError, match="connection_lost"):
            check()
        with other_session_lock(MEDIA_LOCK, 123):
            pass


def test_delete_clip_and_thumbnail_respect_same_lock(test_db):
    with other_session_lock(MEDIA_LOCK, 123):
        with pytest.raises(MediaOperationBusy):
            MediaService.delete_media(123)
        with pytest.raises(MediaOperationBusy):
            MediaClipService.create_clip(123, None)
        assert MediaThumbnailTaskService._generate_one(123).state == "skipped"


def test_busy_thumbnail_preserves_retry_budget(test_db):
    lib = MediaLibrary.create(name="library", provider_key="local", provider_config={})
    video = VideoItem.create(title="video")
    media = Media.create(
        library=lib,
        video_item=video,
        file_name="video.mp4",
        thumbnail_attempt_count=2,
        thumbnail_deferred_count=3,
    )
    with other_session_lock(MEDIA_LOCK, media.id):
        assert MediaThumbnailTaskService._generate_one(media.id).state == "skipped"
    refreshed = Media.get_by_id(media.id)
    assert (
        refreshed.thumbnail_attempt_count == 2
        and refreshed.thumbnail_deferred_count == 3
    )
    assert refreshed.thumbnail_generation_state == Media.THUMBNAIL_STATE_PENDING


def test_scanner_and_library_mutation_skip_busy_library(test_db, monkeypatch):
    lib = MediaLibrary.create(name="library", provider_key="local", provider_config={})
    video = VideoItem.create(title="video")
    media = Media.create(library=lib, video_item=video, file_name="video.mp4")

    def must_not_read(*args):
        raise AssertionError("must not read inventory for busy library")

    monkeypatch.setattr(MediaValidityScanService, "_managed_ref_keys", must_not_read)
    with other_session_lock(LIBRARY_LOCK, lib.id):
        with pytest.raises(MediaOperationBusy):
            MediaLibraryService.update_library(lib.id, None)
        with pytest.raises(MediaOperationBusy):
            MediaLibraryService.delete_library(lib.id)
        result = MediaValidityScanService.scan_media_validity(
            reporter=SimpleNamespace(emit=lambda **kwargs: None)
        )
    assert result["skipped_media"] == 1
    assert Media.get_by_id(media.id).valid


def test_scanner_holds_lock_through_inventory_and_writeback(test_db, monkeypatch):
    lib = MediaLibrary.create(name="library", provider_key="local", provider_config={})
    video = VideoItem.create(title="video")
    media = Media.create(library=lib, video_item=video, file_name="video.mp4")

    def assert_other_session_blocked():
        outcomes = []

        def attempt():
            with get_database().connection_context():
                try:
                    with media_operation_lock(LIBRARY_LOCK, lib.id):
                        outcomes.append("acquired")
                except MediaOperationBusy:
                    outcomes.append("busy")

        thread = Thread(target=attempt)
        thread.start()
        thread.join(10)
        assert outcomes == ["busy"]

    def inventory(library):
        assert_other_session_blocked()

        def key(*, media_ref):
            assert_other_session_blocked()
            return "missing"

        return key, set()

    monkeypatch.setattr(MediaValidityScanService, "_managed_ref_keys", inventory)
    MediaValidityScanService.scan_media_validity(
        reporter=SimpleNamespace(emit=lambda **kwargs: None)
    )
    assert not Media.get_by_id(media.id).valid
    with other_session_lock(LIBRARY_LOCK, lib.id):
        pass


def test_scanner_skips_library_deleted_after_enumeration(test_db, monkeypatch):
    lib = MediaLibrary.create(name="library", provider_key="local", provider_config={})

    @contextmanager
    def delete_before_lock(_namespace, _resource_id):
        MediaLibrary.delete().where(MediaLibrary.id == lib.id).execute()
        yield

    monkeypatch.setattr(
        "src.service.playback.media_validity_scan_service.media_operation_lock",
        delete_before_lock,
    )

    stats = MediaValidityScanService.scan_media_validity(
        reporter=SimpleNamespace(emit=lambda **kwargs: None)
    )

    assert stats["scanned_libraries"] == 0


def test_plugin_management_gate_covers_real_mutation_entries(tmp_path):
    from src.api.exception.errors import ApiError

    manager = PluginManager(root_dir=tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    with plugin_operation_lock(tmp_path, shared=True):
        for operation in (
            lambda: manager.remove("demo"),
            lambda: manager.set_enabled("demo", False),
            lambda: manager._publish_staging(staging, None, enable=False),
        ):
            with pytest.raises(ApiError) as error:
                operation()
            assert error.value.status_code == 409
    assert not staging.exists()
    with (
        plugin_operation_lock(tmp_path),
        pytest.raises(ApiError),
        plugin_operation_lock(tmp_path, shared=True),
    ):
        pytest.fail("transfer must not start during plugin mutation")
