from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.config.config import settings
from src.model import (
    BackgroundTaskRun,
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    Subtitle,
    VideoItem,
)
from src.plugins.provider_protocol import (
    MediaTransferSourceInfo,
    ProviderOperationError,
    StagedMediaTransfer,
)
from src.schema.transfers.media_transfer import (
    MediaStorageTransferCandidatesRequest,
    MediaStorageTransferRequest,
)
from src.service.transfers.shared.media_transfer_task_service import (
    MediaTransferTaskService as Service,
)

TRANSFER_MD5 = "49be0b0bc9c65a0f105a4d14bf812e5d"
TRANSFER_SHA1 = "1a6d82802710806673c06581472421b5d88b09bd"
TRANSFER_GCID = "A" * 40


class Source:
    info = MediaTransferSourceInfo(
        file_name="source.mp4",
        size_bytes=10,
        content_md5=TRANSFER_MD5,
        content_sha1=TRANSFER_SHA1,
        content_gcid=TRANSFER_GCID.lower(),
    )
    changed = False
    opened = False
    cleaned = False
    cleanup_error = False

    @contextmanager
    def open_transfer_source(self, *, media):
        self.original = media
        self.opened = True
        try:
            yield self
        finally:
            self.opened = False

    @contextmanager
    def open_reader(self):
        from io import BytesIO

        yield BytesIO(b"test-bytes")

    def assert_unchanged(self):
        if self.changed:
            raise RuntimeError("SECRET_SENTINEL source changed")

    def cleanup_transfer_source(self, *, media, source):
        assert self.opened and source is self and media == self.original
        self.assert_unchanged()
        if self.cleanup_error:
            raise RuntimeError("SECRET_SENTINEL cleanup failed")
        self.cleaned = True


class Target:
    missed = False
    error = False

    def __init__(self):
        self.staged = 0
        self.finalized = 0
        self.aborted = 0

    def stage_transfer(self, *, source, placement, operation_key):
        assert source.opened
        self.staged += 1
        if self.missed:
            return StagedMediaTransfer(status="not_available")
        return StagedMediaTransfer(
            status="staged",
            storage_ref={"fid": operation_key},
            receipt={"fid": operation_key},
            file_name=source.info.file_name,
            size_bytes=source.info.size_bytes,
        )

    def finalize_transfer(self, *, receipt):
        self.finalized += 1
        if self.error:
            raise RuntimeError("SECRET_SENTINEL target unavailable")

    def abort_transfer(self, *, receipt):
        self.aborted += 1


@pytest.fixture
def move(test_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings.plugins, "root_dir", str(tmp_path / "plugins"))
    source_lib = MediaLibrary.create(
        name="source", provider_key="local", provider_config={}
    )
    target_lib = MediaLibrary.create(
        name="target", provider_key="cloud115", provider_config={}
    )
    video = VideoItem.create(title="source")
    media = Media.create(
        library=source_lib,
        video_item=video,
        storage_ref={"path": "source.mp4"},
        file_name="source.mp4",
        file_size_bytes=10,
        duration_seconds=123,
        resolution="1920x1080",
        video_info={"codec": "h264"},
        file_hash="f" * 64,
        import_source_identity="original",
    )
    source, target = Source(), Target()
    monkeypatch.setattr(
        Service,
        "_storage_for",
        lambda lib: source if lib.id == source_lib.id else target,
    )
    return SimpleNamespace(
        source=source,
        target=target,
        media=media,
        source_lib=source_lib,
        target_lib=target_lib,
    )


def enqueue(move, media_ids=None):
    accepted = Service.enqueue(
        MediaStorageTransferRequest(
            media_ids=media_ids or [move.media.id], target_library_id=move.target_lib.id
        )
    )
    run = BackgroundTaskRun.get_by_id(accepted.task_run_id)
    run.state = "running"
    run.save(only=[BackgroundTaskRun.state])
    return run


def add_following_media(move):
    return Media.create(
        library=move.source_lib,
        video_item=move.media.video_item_id,
        storage_ref={"path": "source.mp4"},
        file_name="source.mp4",
        file_size_bytes=10,
        duration_seconds=123,
        resolution="1920x1080",
        video_info={"codec": "h264"},
        file_hash="e" * 64,
        import_source_identity="following",
    )


def execute(run):
    return Service.execute(SimpleNamespace(task_run_id=run.id, summary={}), run.params)


def test_move_preserves_original_id_and_related_assets(move):
    media = move.media
    image = Image.create(origin="a")
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=10)
    point = MediaPoint.create(media=media, thumbnail=thumbnail, image=image, movie_number=media.movie_number,
        video_item_id=media.video_item_id, offset_seconds=10)
    progress = MediaProgress.create(media=media, position_seconds=42)
    clip = MediaClip.create(
        media=media,
        start_offset_seconds=1,
        end_offset_seconds=3,
        file_path="old-clip.mp4",
    )
    before = {
        type(row): (row.id, dict(type(row).get_by_id(row.id).__data__))
        for row in (thumbnail, point, progress, clip)
    }
    run = enqueue(move)
    result = execute(run)
    changed = Media.get_by_id(media.id)
    assert Media.select().count() == 1
    assert changed.library_id == move.target_lib.id
    assert changed.video_item_id == media.video_item_id
    for field in ("file_hash", "duration_seconds", "resolution", "video_info", "valid"):
        assert getattr(changed, field) == getattr(media, field)
    assert changed.content_md5 == TRANSFER_MD5
    assert changed.content_sha1 == TRANSFER_SHA1
    assert changed.content_gcid == TRANSFER_GCID
    assert changed.import_source_identity is None
    for model, (row_id, fields) in before.items():
        assert model.get_by_id(row_id).__data__ == fields
    assert move.source.cleaned and not move.source.opened
    assert move.target.finalized == 1 and move.target.aborted == 0
    assert result["transferred_count"] == 1 and result["unexecuted_media_ids"] == []
    assert result["files"] == [
        {
            "media_id": media.id,
            "file_name": "source.mp4",
            "movie_number": None,
            "size_bytes": 10,
            "status": "transferred",
            "reason_code": None,
            "target_committed": True,
        }
    ]
    assert Service.list_files(run.id)[0].status == "transferred"
    assert "_current_item" not in BackgroundTaskRun.get_by_id(run.id).params


def test_not_hit_skips_without_switch_or_cleanup(move):
    move.target.missed = True
    original_updated_at = move.media.updated_at
    result = execute(enqueue(move))
    assert result["skipped_count"] == 1
    changed = Media.get_by_id(move.media.id)
    assert changed.library_id == move.source_lib.id
    assert changed.content_md5 == TRANSFER_MD5
    assert changed.content_sha1 == TRANSFER_SHA1
    assert changed.content_gcid == TRANSFER_GCID
    assert changed.updated_at == original_updated_at
    assert not move.source.cleaned
    assert move.target.finalized == move.target.aborted == 0
    assert result["files"][0]["status"] == "skipped"
    assert result["files"][0]["reason_code"] == "target_not_available"
    assert result["files"][0]["target_committed"] is False


def test_not_hit_retry_passes_persisted_hashes_to_source(move):
    move.target.missed = True
    first = enqueue(move)
    execute(first)
    BackgroundTaskRun.update(state="completed", mutex_key=None).where(
        BackgroundTaskRun.id == first.id
    ).execute()

    received = []
    open_source = move.source.open_transfer_source

    def capture_source_handle(*, media):
        received.append(
            (
                media.content_md5,
                media.content_sha1,
                media.content_gcid,
            )
        )
        return open_source(media=media)

    move.source.open_transfer_source = capture_source_handle

    assert execute(enqueue(move))["skipped_count"] == 1
    assert received == [(TRANSFER_MD5, TRANSFER_SHA1, TRANSFER_GCID)]


def test_source_hashes_are_persisted_before_target_stage(move):
    stage = move.target.stage_transfer

    def inspect_hashes(**kwargs):
        changed = Media.get_by_id(move.media.id)
        assert changed.content_md5 == TRANSFER_MD5
        assert changed.content_sha1 == TRANSFER_SHA1
        assert changed.content_gcid == TRANSFER_GCID
        return stage(**kwargs)

    move.target.stage_transfer = inspect_hashes
    assert execute(enqueue(move))["transferred_count"] == 1


def test_stage_transfer_retries_retryable_unavailable(move, monkeypatch):
    monkeypatch.setattr(Service, "STAGE_TRANSFER_RETRY_DELAYS", (0.0, 0.0))
    stage = move.target.stage_transfer
    operation_keys = []

    def flaky(**kwargs):
        operation_keys.append(kwargs["operation_key"])
        if len(operation_keys) < 3:
            raise ProviderOperationError(
                provider_key="test",
                operation="stage_transfer",
                code="unavailable",
                safe_message="transient",
                retryable=True,
            )
        return stage(**kwargs)

    move.target.stage_transfer = flaky
    result = execute(enqueue(move))

    assert len(operation_keys) == 3
    assert len(set(operation_keys)) == 1
    assert result["transferred_count"] == 1
    assert result["issues"] == []


def test_stage_retry_exhausted_continues_next_media(move, monkeypatch):
    monkeypatch.setattr(Service, "STAGE_TRANSFER_RETRY_DELAYS", (0.0, 0.0))
    second = add_following_media(move)
    stage = move.target.stage_transfer
    calls = 0

    def flaky(**kwargs):
        nonlocal calls
        calls += 1
        if calls <= 3:
            raise ProviderOperationError(
                provider_key="test",
                operation="stage_transfer",
                code="unavailable",
                safe_message="transient",
                retryable=True,
            )
        return stage(**kwargs)

    move.target.stage_transfer = flaky
    result = execute(enqueue(move, [move.media.id, second.id]))

    assert calls == 4
    assert result["transferred_count"] == 1
    assert result["failed_count"] == 1
    assert result["unexecuted_media_ids"] == []
    assert result["issues"] == [
        {
            "media_id": move.media.id,
            "reason_code": "stage_failed",
            "target_committed": False,
        }
    ]
    assert [item["status"] for item in result["files"]] == ["failed", "transferred"]


def test_non_retryable_stage_failure_continues_without_retry(move):
    second = add_following_media(move)
    stage = move.target.stage_transfer
    calls = 0

    def fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderOperationError(
                provider_key="test",
                operation="stage_transfer",
                code="invalid_config",
                safe_message="bad stage",
                retryable=False,
            )
        return stage(**kwargs)

    move.target.stage_transfer = fail_once
    result = execute(enqueue(move, [move.media.id, second.id]))

    assert calls == 2
    assert result["transferred_count"] == 1
    assert result["failed_count"] == 1
    assert result["unexecuted_media_ids"] == []


def test_batch_fatal_error_stops_remaining_media(move, monkeypatch):
    second = add_following_media(move)

    def lose_connection(*args):
        raise RuntimeError("media_operation_connection_lost")

    monkeypatch.setattr(Service, "_switch_media", lose_connection)
    run = enqueue(move, [move.media.id, second.id])

    with pytest.raises(RuntimeError, match="未全部完成"):
        execute(run)

    summary = BackgroundTaskRun.get_by_id(run.id).result_summary
    assert summary["failed_count"] == 1
    assert summary["unexecuted_media_ids"] == [second.id]
    assert Media.get_by_id(second.id).library_id == move.source_lib.id
    assert [item["status"] for item in summary["files"]] == ["failed", "unexecuted"]


def test_authentication_failure_stops_batch(move):
    second = add_following_media(move)
    calls = []

    def fail_auth(**kwargs):
        calls.append(kwargs["operation_key"])
        raise ProviderOperationError(
            provider_key="test",
            operation="stage_transfer",
            code="authentication_failed",
            safe_message="login expired",
            retryable=False,
        )

    move.target.stage_transfer = fail_auth
    run = enqueue(move, [move.media.id, second.id])

    with pytest.raises(RuntimeError, match="未全部完成"):
        execute(run)

    assert len(calls) == 1
    summary = BackgroundTaskRun.get_by_id(run.id).result_summary
    assert summary["failed_count"] == 1
    assert summary["unexecuted_media_ids"] == [second.id]
    assert [item["status"] for item in summary["files"]] == ["failed", "unexecuted"]
    assert Media.get_by_id(second.id).library_id == move.source_lib.id


def test_all_failed_items_are_processed_before_task_fails(move):
    second = add_following_media(move)
    calls = []

    def fail(**kwargs):
        calls.append(kwargs["operation_key"])
        raise ProviderOperationError(
            provider_key="test",
            operation="stage_transfer",
            code="invalid_config",
            safe_message="bad stage",
            retryable=False,
        )

    move.target.stage_transfer = fail
    run = enqueue(move, [move.media.id, second.id])

    with pytest.raises(RuntimeError, match="未全部完成"):
        execute(run)

    assert len(calls) == 2
    summary = BackgroundTaskRun.get_by_id(run.id).result_summary
    assert summary["failed_count"] == 2
    assert summary["unexecuted_media_ids"] == []
    assert [item["status"] for item in summary["files"]] == ["failed", "failed"]


def test_source_without_hashes_keeps_cache_empty(move):
    move.source.info = MediaTransferSourceInfo(file_name="source.mp4", size_bytes=10)

    assert execute(enqueue(move))["transferred_count"] == 1

    changed = Media.get_by_id(move.media.id)
    assert changed.content_md5 is None
    assert changed.content_sha1 is None
    assert changed.content_gcid is None


@pytest.mark.parametrize(
    ("info", "expected_md5", "expected_sha1"),
    (
        (
            MediaTransferSourceInfo(
                file_name="source.mp4", size_bytes=10, content_md5=TRANSFER_MD5
            ),
            TRANSFER_MD5,
            None,
        ),
        (
            MediaTransferSourceInfo(
                file_name="source.mp4", size_bytes=10, content_sha1=TRANSFER_SHA1
            ),
            None,
            TRANSFER_SHA1,
        ),
    ),
)
def test_source_hashes_are_cached_independently(
    move, info, expected_md5, expected_sha1
):
    move.source.info = info

    assert execute(enqueue(move))["transferred_count"] == 1

    changed = Media.get_by_id(move.media.id)
    assert changed.content_md5 == expected_md5
    assert changed.content_sha1 == expected_sha1
    assert changed.content_gcid is None


def test_invalid_source_hash_stops_before_stage(move):
    move.source.info = MediaTransferSourceInfo(
        file_name="source.mp4",
        size_bytes=10,
        content_md5="invalid",
    )
    run = enqueue(move)

    with pytest.raises(RuntimeError):
        execute(run)

    assert move.target.staged == 0
    changed = Media.get_by_id(move.media.id)
    assert changed.content_md5 is None
    assert changed.content_sha1 is None
    assert changed.content_gcid is None


def test_source_with_only_gcid_caches_it(move):
    move.source.info = MediaTransferSourceInfo(
        file_name="source.mp4",
        size_bytes=10,
        content_gcid=TRANSFER_GCID.lower(),
    )

    assert execute(enqueue(move))["transferred_count"] == 1

    changed = Media.get_by_id(move.media.id)
    assert changed.content_md5 is None
    assert changed.content_sha1 is None
    assert changed.content_gcid == TRANSFER_GCID


def test_invalid_gcid_stops_before_stage(move):
    move.source.info = MediaTransferSourceInfo(
        file_name="source.mp4",
        size_bytes=10,
        content_gcid="invalid",
    )
    run = enqueue(move)

    with pytest.raises(RuntimeError):
        execute(run)

    assert move.target.staged == 0
    changed = Media.get_by_id(move.media.id)
    assert changed.content_gcid is None


def test_changed_source_only_aborts_before_switch(move):
    move.source.changed = True
    run = enqueue(move)
    with pytest.raises(RuntimeError, match="未全部完成"):
        execute(run)
    assert move.target.aborted == 1 and not move.source.cleaned
    assert Media.get_by_id(move.media.id).library_id == move.source_lib.id
    assert (
        BackgroundTaskRun.get_by_id(run.id).result_summary["issues"][0]["reason_code"]
        == "source_changed"
    )


@pytest.mark.parametrize("commit", [False, True])
def test_switch_error_never_aborts_even_when_commit_ack_is_lost(
    move, monkeypatch, commit
):
    original = Service._switch_media

    def fail(*args):
        if commit:
            original(*args)
        raise RuntimeError("commit ACK lost")

    monkeypatch.setattr(Service, "_switch_media", fail)
    run = enqueue(move)
    with pytest.raises(RuntimeError):
        execute(run)
    assert move.target.aborted == 0 and not move.source.cleaned
    assert Media.get_by_id(move.media.id).library_id == (
        move.target_lib.id if commit else move.source_lib.id
    )
    summary = BackgroundTaskRun.get_by_id(run.id).result_summary
    assert summary["cleanup_incomplete_count" if commit else "failed_count"] == 1


def test_media_switch_and_phase_rollback_together(move, monkeypatch):
    save = BackgroundTaskRun.save

    def fail_phase(self, *args, **kwargs):
        if (self.params or {}).get("_current_item", {}).get(
            "phase"
        ) == "media_switched":
            raise RuntimeError("phase write failed")
        return save(self, *args, **kwargs)

    monkeypatch.setattr(BackgroundTaskRun, "save", fail_phase)
    run = enqueue(move)
    with pytest.raises(RuntimeError):
        execute(run)
    assert Media.get_by_id(move.media.id).library_id == move.source_lib.id
    assert move.target.aborted == 0 and not move.source.cleaned
    assert (
        BackgroundTaskRun.get_by_id(run.id).result_summary["issues"][0][
            "target_committed"
        ]
        is False
    )


@pytest.mark.parametrize("failure", ["target", "cleanup"])
def test_post_switch_failure_retains_target_and_continues_batch(move, failure):
    second = add_following_media(move)
    original_finalize = move.target.finalize_transfer
    original_cleanup = move.source.cleanup_transfer_source
    calls = {"finalize": 0, "cleanup": 0}

    def fail_first_finalize(**kwargs):
        calls["finalize"] += 1
        if calls["finalize"] == 1:
            raise RuntimeError("SECRET_SENTINEL target unavailable")
        original_finalize(**kwargs)

    def fail_first_cleanup(**kwargs):
        calls["cleanup"] += 1
        if calls["cleanup"] == 1:
            raise RuntimeError("SECRET_SENTINEL cleanup failed")
        original_cleanup(**kwargs)

    if failure == "target":
        move.target.finalize_transfer = fail_first_finalize
    else:
        move.source.cleanup_transfer_source = fail_first_cleanup
    run = enqueue(move, [move.media.id, second.id])
    result = execute(run)
    assert move.target.staged == 2 and move.target.aborted == 0
    assert move.source.cleaned
    assert Media.get_by_id(move.media.id).library_id == move.target_lib.id
    assert Media.get_by_id(second.id).library_id == move.target_lib.id
    assert result["transferred_count"] == 1
    assert result["cleanup_incomplete_count"] == 1
    assert result["unexecuted_media_ids"] == []
    assert [item["status"] for item in result["files"]] == [
        "cleanup_incomplete",
        "transferred",
    ]
    assert result["files"][0]["reason_code"] in {
        "target_verification_failed",
        "source_cleanup_failed",
    }
    assert result["files"][0]["target_committed"] is True
    assert "SECRET_SENTINEL" not in str(result)


def test_completed_items_survive_later_failure(move):
    second = add_following_media(move)
    finalize = move.target.finalize_transfer

    def fail_second(**kwargs):
        if move.target.staged == 2:
            raise RuntimeError("second target failed")
        finalize(**kwargs)

    move.target.finalize_transfer = fail_second
    run = enqueue(move, [move.media.id, second.id])
    result = execute(run)
    summary = result
    assert (
        summary["transferred_count"] == 1 and summary["cleanup_incomplete_count"] == 1
    )
    assert summary["unexecuted_media_ids"] == []
    assert [item["status"] for item in summary["files"]] == [
        "transferred",
        "cleanup_incomplete",
    ]


def test_crash_after_unlink_is_only_reconciled_never_retried(move):
    cleanup = move.source.cleanup_transfer_source

    def crash(**kwargs):
        cleanup(**kwargs)
        raise SystemExit("simulated process death")

    move.source.cleanup_transfer_source = crash
    run = enqueue(move)
    with pytest.raises(SystemExit):
        execute(run)
    assert move.source.cleaned
    BackgroundTaskRun.update(state="failed", mutex_key=None).where(
        BackgroundTaskRun.id == run.id
    ).execute()
    assert Service.recover_interrupted_transfers() == {"interrupted_runs": 1}
    assert Service.recover_interrupted_transfers() == {"interrupted_runs": 0}
    summary = BackgroundTaskRun.get_by_id(run.id).result_summary
    assert summary["issues"][0]["reason_code"] == "cleanup_unconfirmed"
    assert move.target.staged == move.target.finalized == 1 and move.target.aborted == 0


def test_configuration_changed_while_pending_prevents_stage(move):
    run = enqueue(move)
    move.source_lib.name = "renamed"
    move.source_lib.save()
    with pytest.raises(RuntimeError):
        execute(run)
    assert move.target.staged == 0
    assert (
        BackgroundTaskRun.get_by_id(run.id).result_summary["reason_code"]
        == "library_changed"
    )


def test_legacy_receipts_are_never_replayed_or_silently_ignored(move):
    old = BackgroundTaskRun.create(
        task_key=Service.TASK_KEY,
        task_name="old-copy",
        trigger_type="manual",
        state="failed",
        params={"_staged_transfers": {"old": {"receipt": "opaque"}}},
    )
    assert Service.recover_interrupted_transfers() == {"interrupted_runs": 0}
    with pytest.raises(Exception, match="旧媒体复制任务"):
        enqueue(move)
    assert BackgroundTaskRun.get_by_id(old.id).params["_staged_transfers"]
    assert move.target.aborted == move.target.finalized == 0


def test_task_lease_loss_during_stage_prevents_switch_and_cleanup(move):
    run = enqueue(move)
    stage = move.target.stage_transfer

    def expire(**kwargs):
        result = stage(**kwargs)
        BackgroundTaskRun.update(state="failed", mutex_key=None).where(
            BackgroundTaskRun.id == run.id
        ).execute()
        return result

    move.target.stage_transfer = expire
    with pytest.raises(RuntimeError):
        execute(run)
    assert not move.source.cleaned
    assert Media.get_by_id(move.media.id).library_id == move.source_lib.id
    assert move.target.aborted == 0


def test_enqueue_requires_cleanup_capability(move):
    move.source.cleanup_transfer_source = None
    with pytest.raises(Exception, match="不支持迁移"):
        enqueue(move)
    assert BackgroundTaskRun.select().count() == 0


def test_candidates_and_enqueue_use_provider_capabilities_not_provider_keys(move):
    move.source_lib.provider_key = "source-provider"
    move.source_lib.save()
    move.target_lib.provider_key = "target-provider"
    move.target_lib.save()

    candidates = Service.list_candidates(
        MediaStorageTransferCandidatesRequest(media_ids=[move.media.id])
    )

    assert candidates.source_library.id == move.source_lib.id
    assert [(target.id, target.name) for target in candidates.targets] == [
        (move.target_lib.id, move.target_lib.name)
    ]
    assert execute(enqueue(move))["transferred_count"] == 1


def test_candidates_exclude_libraries_without_target_capability(move, monkeypatch):
    unsupported = MediaLibrary.create(
        name="unsupported", provider_key="readonly", provider_config={}
    )

    class ReadOnlyStorage:
        pass

    monkeypatch.setattr(
        Service,
        "_storage_for",
        lambda library: (
            move.source
            if library.id == move.source_lib.id
            else move.target
            if library.id == move.target_lib.id
            else ReadOnlyStorage()
        ),
    )

    candidates = Service.list_candidates(
        MediaStorageTransferCandidatesRequest(media_ids=[move.media.id])
    )

    assert [target.id for target in candidates.targets] == [move.target_lib.id]
    assert unsupported.id not in [target.id for target in candidates.targets]


def test_enqueue_queues_behind_existing_target_import(move):
    first = enqueue(move)
    assert first.scope_key == f"library_import:{move.target_lib.id}"

    accepted = Service.enqueue(
        MediaStorageTransferRequest(
            media_ids=[move.media.id],
            target_library_id=move.target_lib.id,
        )
    )
    queued = BackgroundTaskRun.get_by_id(accepted.task_run_id)
    assert queued.state == "pending"
    assert queued.scope_key == first.scope_key


@pytest.mark.parametrize(
    "payload",
    [
        {"media_ids": [True], "target_library_id": 2},
        {"media_ids": [1], "target_library_id": True},
    ],
)
def test_request_rejects_boolean_ids(payload):
    with pytest.raises(ValueError):
        MediaStorageTransferRequest.model_validate(payload)


def test_movie_and_external_subtitle_remain_attached(move, tmp_path):
    movie = Movie.create(javdb_id="move-test", movie_number="MOVE-001", title="movie")
    subtitle_path = tmp_path / "movie.srt"
    subtitle_path.write_text("original subtitle")
    subtitle = Subtitle.create(movie=movie, file_path=str(subtitle_path))
    Media.update(movie_number=movie.movie_number, video_item=None).where(
        Media.id == move.media.id
    ).execute()
    assert execute(enqueue(move))["transferred_count"] == 1
    assert Media.get_by_id(move.media.id).movie_number == movie.movie_number
    assert Subtitle.get_by_id(subtitle.id).movie_id == movie.id
    assert subtitle_path.read_text() == "original subtitle"


def test_busy_library_prevents_provider_work(move):
    from src.service.playback.operation_locks import LIBRARY_LOCK
    from tests.service.test_media_operation_locks import other_session_lock

    run = enqueue(move)
    with (
        other_session_lock(LIBRARY_LOCK, move.source_lib.id),
        pytest.raises(RuntimeError),
    ):
        execute(run)
    assert move.target.staged == 0 and not move.source.opened
    summary = BackgroundTaskRun.get_by_id(run.id).result_summary
    assert summary["reason_code"] == "media_busy"
    assert summary["unexecuted_media_ids"] == [move.media.id]
    assert summary["files"][0]["status"] == "unexecuted"


def test_busy_media_is_skipped_and_batch_continues(move):
    from src.service.playback.operation_locks import MEDIA_LOCK
    from tests.service.test_media_operation_locks import other_session_lock

    busy = move.media
    following = Media.create(
        library=move.source_lib,
        video_item=VideoItem.create(title="following"),
        storage_ref={"path": "source.mp4"},
        file_name="source.mp4",
        file_size_bytes=10,
        duration_seconds=123,
        resolution="1920x1080",
        video_info={"codec": "h264"},
        file_hash="e" * 64,
        import_source_identity="following",
    )
    run = enqueue(move, [busy.id, following.id])
    with other_session_lock(MEDIA_LOCK, busy.id):
        result = execute(run)

    assert result["transferred_count"] == 1
    assert result["failed_count"] == 1
    assert result["unexecuted_media_ids"] == []
    assert result["reason_code"] == "media_busy"
    assert result["issues"] == [
        {"media_id": busy.id, "reason_code": "media_busy", "target_committed": False}
    ]
    assert [item["status"] for item in result["files"]] == ["failed", "transferred"]
    assert result["files"][0]["reason_code"] == "media_busy"
    assert Media.get_by_id(busy.id).library_id == move.source_lib.id
    assert Media.get_by_id(following.id).library_id == move.target_lib.id
    persisted = BackgroundTaskRun.get_by_id(run.id)
    assert persisted.progress_current == 2
    assert persisted.progress_total == 2
    assert persisted.params["_move_index"] == 2
    assert "_current_item" not in persisted.params


def test_changed_media_is_recorded_as_failed_item(move):
    run = enqueue(move)
    Media.update(library=move.target_lib.id).where(Media.id == move.media.id).execute()

    with pytest.raises(RuntimeError, match="未全部完成"):
        execute(run)

    summary = BackgroundTaskRun.get_by_id(run.id).result_summary
    assert summary["failed_count"] == 1
    assert summary["unexecuted_media_ids"] == []
    assert summary["issues"] == [
        {
            "media_id": move.media.id,
            "reason_code": "media_unavailable",
            "target_committed": False,
        }
    ]


def test_changed_media_failure_continues_next_media(move):
    second = add_following_media(move)
    run = enqueue(move, [move.media.id, second.id])
    Media.update(library=move.target_lib.id).where(Media.id == move.media.id).execute()

    result = execute(run)

    assert result["transferred_count"] == 1
    assert result["failed_count"] == 1
    assert result["unexecuted_media_ids"] == []
    assert result["issues"] == [
        {
            "media_id": move.media.id,
            "reason_code": "media_unavailable",
            "target_committed": False,
        }
    ]
    assert [item["status"] for item in result["files"]] == ["failed", "transferred"]


def test_lost_batch_connection_cannot_be_replaced_by_new_media_lock(move, monkeypatch):
    from src.model import get_database

    run = enqueue(move)

    def storage_for(library):
        get_database().close()
        return move.source if library.id == move.source_lib.id else move.target

    monkeypatch.setattr(Service, "_storage_for", storage_for)
    with pytest.raises(RuntimeError):
        execute(run)
    assert move.target.staged == 0 and not move.source.cleaned
    assert Media.get_by_id(move.media.id).library_id == move.source_lib.id
