from types import SimpleNamespace

import pytest

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.playback.media_video_info_backfill_service import (
    MediaVideoInfoBackfillService,
)

INFO = {
    "container": {"duration_seconds": 120},
    "video": {"width": 1920, "height": 1080, "codec_name": "h264"},
    "audio": None,
    "subtitles": [],
}


class Reporter:
    def __init__(self):
        self.events = []

    def emit(self, **payload):
        self.events.append({**payload, "summary_patch": dict(payload["summary_patch"])})


@pytest.fixture
def library(test_db):
    return MediaLibrary.create(name="backfill", provider_key="demo", provider_config={})


def _media(library, number, **values):
    movie = Movie.create(movie_number=number, title=number)
    return Media.create(
        movie=movie,
        library=library,
        storage_ref={"path": f"{number}.mp4"},
        file_name=f"{number}.mp4",
        file_size_bytes=100,
        **values,
    )


def _storage(monkeypatch, probe):
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _library: SimpleNamespace(probe_video_info=probe),
    )


def _run(reporter=None):
    return MediaVideoInfoBackfillService.backfill_missing_video_infos(
        reporter=reporter or Reporter()
    )


def test_backfill_fills_each_missing_field_once_and_preserves_existing(
    library, monkeypatch
):
    empty = _media(library, "EMPTY")
    info_only = _media(library, "INFO", video_info={"video": {"codec_name": "hevc"}})
    duration_only = _media(library, "DURATION", duration_seconds=60)
    resolution_only = _media(library, "RESOLUTION", resolution="3840x2160")
    whitespace = _media(
        library, "WHITESPACE", video_info=INFO, duration_seconds=120, resolution="  "
    )
    negative = _media(
        library,
        "NEGATIVE",
        video_info=INFO,
        duration_seconds=-1,
        resolution="1920x1080",
    )
    complete = _media(
        library, "COMPLETE", video_info=INFO, duration_seconds=60, resolution="720x480"
    )
    invalid = _media(library, "INVALID", valid=False)
    calls = []

    def probe(*, media):
        calls.append(media.media_id)
        return INFO

    _storage(monkeypatch, probe)
    reporter = Reporter()
    stats = _run(reporter)
    assert stats == {
        "missing_media": 6,
        "updated_media": 6,
        "failed_media": 0,
        "skipped_media": 0,
        "incomplete_media": 0,
    }
    assert calls == [
        empty.id,
        info_only.id,
        duration_only.id,
        resolution_only.id,
        whitespace.id,
        negative.id,
    ]
    for original in (
        empty,
        info_only,
        duration_only,
        resolution_only,
        whitespace,
        negative,
    ):
        saved = Media.get_by_id(original.id)
        assert saved.video_info == INFO
        assert saved.duration_seconds == (
            60 if original.id == duration_only.id else 120
        )
        assert saved.resolution == (
            "3840x2160" if original.id == resolution_only.id else "1920x1080"
        )
    assert Media.get_by_id(complete.id).duration_seconds == 60
    assert Media.get_by_id(invalid.id).video_info is None
    assert reporter.events[0]["current"] == 0
    assert reporter.events[-1]["current"] == reporter.events[-1]["total"] == 6
    assert reporter.events[-1]["summary_patch"] == stats
    assert _run()["missing_media"] == 0
    assert len(calls) == 6


@pytest.mark.parametrize("result", [None, {}, "invalid", RuntimeError("probe failed")])
def test_failed_probe_does_not_write_and_continues(library, monkeypatch, result):
    failed = _media(library, "FAILED")
    success = _media(library, "SUCCESS")

    def probe(*, media):
        if media.media_id == failed.id:
            if isinstance(result, Exception):
                raise result
            return result
        return INFO

    _storage(monkeypatch, probe)
    stats = _run()
    assert stats["failed_media"] == stats["updated_media"] == 1
    assert Media.get_by_id(failed.id).video_info is None
    assert Media.get_by_id(failed.id).duration_seconds == 0
    assert Media.get_by_id(success.id).resolution == "1920x1080"


def test_provider_without_unified_probe_is_skipped(library, monkeypatch):
    media = _media(library, "LEGACY")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _library: object()
    )
    assert _run()["skipped_media"] == 1
    assert Media.get_by_id(media.id).video_info is None


@pytest.mark.parametrize(
    "duration,width,height",
    [(None, None, None), (True, True, 1080), (-1, 0, -1), (1.5, "bad", 1080)],
)
def test_partial_metadata_is_reported_and_missing_fields_can_be_retried(
    library, monkeypatch, duration, width, height
):
    media = _media(library, "PARTIAL")
    partial = {
        "container": {"duration_seconds": duration},
        "video": {"width": width, "height": height},
    }
    _storage(monkeypatch, lambda **_kwargs: partial)
    reporter = Reporter()
    stats = _run(reporter)
    assert stats["updated_media"] == stats["incomplete_media"] == 1
    assert "仍缺失 1" in reporter.events[-1]["text"]
    saved = Media.get_by_id(media.id)
    assert saved.duration_seconds == 0 and saved.resolution is None
    assert _run()["failed_media"] == 1
    _storage(monkeypatch, lambda **_kwargs: INFO)
    assert _run()["incomplete_media"] == 0
    saved = Media.get_by_id(media.id)
    assert saved.video_info == INFO
    assert saved.duration_seconds == 120 and saved.resolution == "1920x1080"


@pytest.mark.parametrize(
    "changed",
    [
        {"video_info": {"video": {"codec_name": "hevc"}}},
        {"duration_seconds": 77},
        {"resolution": "720x480"},
        {"video_info": INFO, "duration_seconds": 77, "resolution": "720x480"},
        {"valid": False},
    ],
)
def test_probe_concurrent_changes_are_preserved(library, monkeypatch, changed):
    original = _media(library, "CONCURRENT")

    def probe(*, media):
        Media.update(**changed).where(Media.id == media.media_id).execute()
        return INFO

    _storage(monkeypatch, probe)
    stats = _run()
    saved = Media.get_by_id(original.id)
    for field, value in changed.items():
        assert getattr(saved, field) == value
    if not saved.valid or len(changed) == 3:
        assert stats["skipped_media"] == 1
    else:
        assert stats["updated_media"] == 1
        assert saved.duration_seconds > 0 and saved.resolution and saved.video_info


def test_busy_media_is_skipped_and_other_media_continues(library, monkeypatch):
    from contextlib import contextmanager

    from src.service.playback import media_video_info_backfill_service as module
    from src.service.playback.operation_locks import MediaOperationBusy

    busy = _media(library, "BUSY")
    other = _media(library, "OTHER")
    original_lock = module.media_operation_lock

    @contextmanager
    def lock(namespace, media_id):
        if media_id == busy.id:
            raise MediaOperationBusy()
        with original_lock(namespace, media_id):
            yield

    monkeypatch.setattr(module, "media_operation_lock", lock)
    _storage(monkeypatch, lambda **_kwargs: INFO)
    stats = _run()
    assert stats["skipped_media"] == stats["updated_media"] == 1
    assert Media.get_by_id(busy.id).video_info is None
    assert Media.get_by_id(other.id).video_info == INFO


@pytest.mark.parametrize(
    "old,new,replace",
    [
        ({"video": {"width": 1280}}, INFO, True),
        ({}, INFO, True),
        (INFO, {**INFO, "audio": {"codec_name": "aac"}}, True),
        (INFO, {**INFO, "subtitles": [{"codec_name": "ass"}]}, True),
        (INFO, {**INFO, "video": {**INFO["video"], "codec_name": "hevc"}}, False),
        (INFO, {"video": {"width": 1920}}, False),
        ({"audio": {"codec_name": "aac"}}, INFO, False),
        (
            INFO,
            {
                **INFO,
                "video": {
                    **INFO["video"], "profile": None, "pixel_format": "  ", "bit_rate": 0,
                },
                "container": {**INFO["container"], "bit_rate_estimated": True},
            },
            False,
        ),
        (
            {**INFO, "subtitles": [{"codec_name": "ass", "language": "chi"}]},
            {**INFO, "subtitles": [{"codec_name": "ass"}, {"codec_name": "srt"}]},
            False,
        ),
    ],
)
def test_video_info_replaced_only_when_effective_fields_are_a_strict_superset(
    library, monkeypatch, old, new, replace
):
    media = _media(library, "COMPLETENESS", video_info=old)
    _storage(monkeypatch, lambda **_kwargs: new)
    _run()
    assert Media.get_by_id(media.id).video_info == (new if replace else old)


def test_complete_media_is_not_probed_for_richer_video_info(library, monkeypatch):
    old = {"video": {"width": 1920}}
    media = _media(
        library, "COMPLETE", video_info=old,
        duration_seconds=120, resolution="1920x1080",
    )

    def probe(**_kwargs):
        pytest.fail("complete media must not be probed")

    _storage(monkeypatch, probe)
    assert _run()["missing_media"] == 0
    assert Media.get_by_id(media.id).video_info == old


def test_richer_video_info_does_not_overwrite_concurrent_replacement(library, monkeypatch):
    media = _media(library, "CONCURRENT-INFO", video_info={"video": {"width": 1920}})
    concurrent = {"video": {"width": 3840, "codec_name": "hevc"}}

    def probe(**_kwargs):
        Media.update(video_info=concurrent).where(Media.id == media.id).execute()
        return INFO

    _storage(monkeypatch, probe)
    assert _run()["updated_media"] == 1
    saved = Media.get_by_id(media.id)
    assert saved.video_info == concurrent
    assert saved.duration_seconds == 120 and saved.resolution == "1920x1080"
