from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.model import Media, MediaLibrary, Movie, VideoItem
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeResult
from src.service.playback.thumbnails.contracts import ThumbnailDeferred
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService
from src.service.transfers.cloud115.importer.types import CloudSourceFile
from src.service.transfers.imports.writer import upsert_media
from src.service.videos.cloud115_video_import_service import Cloud115VideoImportService


class _Reporter:
    def emit(self, **_kwargs) -> None:
        return None


def _create_media() -> Media:
    library = MediaLibrary.create(
        name="thumbnail-state-library",
        backend="local",
        backend_config={"root_path": "/library"},
    )
    movie = Movie.create(movie_number="THUMB-001", javdb_id="thumb-1", title="thumb")
    return Media.create(
        movie=movie,
        library=library,
        path="/library/THUMB-001.mp4",
        content_fingerprint="thumbnail-state-fingerprint",
    )


def test_thumbnail_generic_failure_reaches_terminal_without_automatic_reopen(
    test_db, monkeypatch
) -> None:
    media = _create_media()
    attempts: list[int] = []

    def fail_generation(cls, current_media: Media) -> int:
        attempts.append(current_media.id)
        raise RuntimeError("thumbnail_decoder_error temporary decoder failure")

    monkeypatch.setattr(
        MediaThumbnailTaskService,
        "generate_for_media",
        classmethod(fail_generation),
    )
    monkeypatch.setattr(
        "src.service.playback.thumbnails.task_service.ThumbnailBackendRegistry.ensure_available",
        lambda _backend: None,
    )

    first = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=_Reporter())
    stored = Media.get_by_id(media.id)
    assert first["retryable_failed_media"] == 1
    assert stored.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT
    assert stored.thumbnail_attempt_count == 1
    assert stored.thumbnail_next_retry_at is not None

    Media.update(thumbnail_next_retry_at=None).where(Media.id == media.id).execute()
    second = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=_Reporter())
    stored = Media.get_by_id(media.id)
    assert second["terminal_failed_media"] == 1
    assert second["terminal_failed_media_ids"] == [media.id]
    assert stored.thumbnail_generation_state == Media.THUMBNAIL_STATE_TERMINAL
    assert stored.thumbnail_attempt_count == 2

    third = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=_Reporter())
    assert third["pending_media"] == 0
    assert attempts == [media.id, media.id]

def test_thumbnail_deferred_state_has_finite_upper_bound(test_db, monkeypatch) -> None:
    media = _create_media()

    def defer_generation(cls, _media: Media) -> int:
        raise ThumbnailDeferred(
            "115 视频转码尚未完成",
            error_code="cloud115_video_transcoding",
            max_deferred_attempts=1,
            deferred_backoff_base_seconds=60,
        )

    monkeypatch.setattr(
        MediaThumbnailTaskService,
        "generate_for_media",
        classmethod(defer_generation),
    )
    monkeypatch.setattr(
        "src.service.playback.thumbnails.task_service.ThumbnailBackendRegistry.ensure_available",
        lambda _backend: None,
    )

    first = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=_Reporter())
    stored = Media.get_by_id(media.id)
    assert first["deferred_media"] == 1
    assert stored.thumbnail_generation_state == Media.THUMBNAIL_STATE_RETRY_WAIT
    assert stored.thumbnail_deferred_count == 1

    Media.update(thumbnail_next_retry_at=None).where(Media.id == media.id).execute()
    second = MediaThumbnailTaskService.generate_pending_thumbnails(reporter=_Reporter())
    stored = Media.get_by_id(media.id)
    assert second["terminal_failed_media"] == 1
    assert stored.thumbnail_generation_state == Media.THUMBNAIL_STATE_TERMINAL
    assert stored.thumbnail_deferred_count == 2
    assert stored.thumbnail_last_error_code == "cloud115_video_transcoding"


def test_cloud115_video_resume_revives_terminal_thumbnail_state(test_db) -> None:
    library = MediaLibrary.create(
        name="thumbnail-cloud115-library",
        backend="cloud115",
        backend_config={"root_cid": "root"},
    )
    video = VideoItem.create(title="restored video")
    media = Media.create(
        video_item=video,
        library=library,
        backend_locator={
            "fid": "restored-fid",
            "pickcode": "restored-pickcode",
            "name": "restored.mp4",
            "source_path": "restored.mp4",
        },
        content_fingerprint="sha1:RESTORED",
        valid=False,
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_attempt_count=2,
        thumbnail_last_error_code="video_stream_missing",
    )
    source = CloudSourceFile(
        fid="restored-fid",
        pickcode="restored-pickcode",
        name="restored.mp4",
        sha1="RESTORED",
        size=1024,
        play_long=30,
        censored=False,
        rel_dir_parts=(),
    )

    class _Client:
        async def move_files(self, _fids, *, pid: str) -> None:
            assert pid == "version-cid"

    class _Resolver:
        async def prepare_videos_version_dir(self, *, video_id: int, now_ms: int) -> str:
            assert video_id == video.id
            assert now_ms > 0
            return "version-cid"

    stats = {"imported": 0, "skipped": 0, "failed": 0}
    asyncio.run(
        Cloud115VideoImportService()._move_registered_source(
            _Client(),
            media=media,
            source=source,
            target_dir_resolver=_Resolver(),
            failure_items=[],
            stats=stats,
        )
    )

    stored = Media.get_by_id(media.id)
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert stored.valid is True
    assert stored.thumbnail_generation_state == Media.THUMBNAIL_STATE_PENDING
    assert stored.thumbnail_attempt_count == 0
    assert stored.thumbnail_last_error_code is None


def test_local_import_revival_resets_terminal_thumbnail_state(test_db, tmp_path) -> None:
    library = MediaLibrary.create(
        name="thumbnail-import-library",
        backend="local",
        backend_config={"root_path": str(tmp_path)},
    )
    movie = Movie.create(
        movie_number="THUMB-IMPORT-001",
        javdb_id="thumb-import-1",
        title="thumb import",
    )
    media = Media.create(
        movie=movie,
        library=library,
        path=str(tmp_path / "missing.mp4"),
        content_fingerprint="import-revival-fingerprint",
        valid=False,
        thumbnail_generation_state=Media.THUMBNAIL_STATE_TERMINAL,
        thumbnail_attempt_count=2,
        thumbnail_last_error_code="video_file_missing",
    )
    target_path = tmp_path / "restored.mp4"
    target_path.write_bytes(b"restored video")
    probe_service = SimpleNamespace(
        probe_file=lambda _path: MediaMetadataProbeResult(
            resolution="1920x1080",
            duration_seconds=60,
            video_info={"video": {"width": 1920, "height": 1080}},
        )
    )

    upsert_media(
        movie=movie,
        library=library,
        target_path=target_path,
        storage_mode="copy",
        content_fingerprint="import-revival-fingerprint",
        file_size=target_path.stat().st_size,
        special_tag_source_paths=[target_path],
        has_sidecar_subtitle=False,
        media_metadata_probe_service=probe_service,
    )

    stored = Media.get_by_id(media.id)
    assert stored.valid is True
    assert stored.path == str(target_path)
    assert stored.thumbnail_generation_state == Media.THUMBNAIL_STATE_PENDING
    assert stored.thumbnail_attempt_count == 0
    assert stored.thumbnail_last_error_code is None
