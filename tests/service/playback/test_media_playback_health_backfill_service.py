from src.model import Media, MediaLibrary, Movie, SystemNotification
from src.service.playback.media_playback_health_backfill_service import (
    MediaPlaybackHealthBackfillService,
)
from src.service.playback.media_playback_health_service import (
    REASON_INTERLEAVE_BAD,
    STATUS_INVALID,
    STATUS_OK,
    PlaybackHealth,
)


class Reporter:
    task_run_id = None

    def __init__(self) -> None:
        self.events: list[tuple[int, int, dict]] = []
        self.texts: list[str | None] = []

    def emit(
        self,
        *,
        current: int,
        total: int,
        summary_patch: dict,
        text: str | None = None,
    ) -> None:
        self.events.append((current, total, dict(summary_patch)))
        self.texts.append(text)


def _media(
    library: MediaLibrary,
    number: str,
    *,
    health=None,
    valid: bool = True,
) -> Media:
    movie = Movie.create(
        movie_number=number,
        javdb_id=f"javdb-{number}",
        title=number,
    )
    return Media.create(
        movie=movie,
        library=library,
        file_name=f"{number}.mp4",
        file_size_bytes=100,
        playback_health=health,
        valid=valid,
    )


def test_backfill_only_probes_missing_health_and_notifies_invalid(
    test_db,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="health-backfill",
        provider_key="demo",
        provider_config={},
    )
    missing = _media(library, "BACKFILL-001")
    invalid = _media(library, "BACKFILL-INVALID", valid=False)
    existing = _media(
        library,
        "BACKFILL-002",
        health={"status": "ok", "reason": None, "detail": {}},
    )
    probed = []

    def fake_probe_and_write(media, *, storage=None):
        probed.append(media.id)
        health = PlaybackHealth(
            status=STATUS_INVALID,
            reason=REASON_INTERLEAVE_BAD,
            detail={"max_gap_mib": 1024},
        )
        media.playback_health = health.to_json()
        media.valid = False
        media.save(only=[Media.playback_health, Media.valid])
        return health, True

    monkeypatch.setattr(
        "src.service.playback.media_playback_health_backfill_service."
        "MEDIA_PROVIDER_REGISTRY.storage_for",
        lambda _library: object(),
    )
    monkeypatch.setattr(
        "src.service.playback.media_playback_health_backfill_service."
        "MediaPlaybackHealthService.probe_and_write",
        fake_probe_and_write,
    )

    stats = MediaPlaybackHealthBackfillService.backfill_pending_health(
        reporter=Reporter()
    )

    assert probed == [missing.id]
    assert invalid.id not in probed
    assert Media.get_by_id(existing.id).playback_health["status"] == "ok"
    assert stats == {
        "candidate_media": 1,
        "updated_media": 1,
        "invalid_media": 1,
        "ok_media": 0,
        "unknown_media": 0,
        "skipped_media": 0,
        "failed_media": 0,
    }
    notification = SystemNotification.get(
        SystemNotification.event_type == "media_invalid_detected"
    )
    assert "BACKFILL-001" in notification.content


def test_backfill_counts_ok_and_continues_after_failure(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="health-backfill",
        provider_key="demo",
        provider_config={},
    )
    failed = _media(library, "BACKFILL-001")
    _media(library, "BACKFILL-002")

    def fake_probe_and_write(media, *, storage=None):
        if media.id == failed.id:
            raise RuntimeError("probe crashed")
        health = PlaybackHealth(status=STATUS_OK)
        media.playback_health = health.to_json()
        media.save(only=[Media.playback_health])
        return health, False

    monkeypatch.setattr(
        "src.service.playback.media_playback_health_backfill_service."
        "MEDIA_PROVIDER_REGISTRY.storage_for",
        lambda _library: object(),
    )
    monkeypatch.setattr(
        "src.service.playback.media_playback_health_backfill_service."
        "MediaPlaybackHealthService.probe_and_write",
        fake_probe_and_write,
    )

    stats = MediaPlaybackHealthBackfillService.backfill_pending_health(
        reporter=Reporter()
    )

    assert stats["updated_media"] == 1
    assert stats["ok_media"] == 1
    assert stats["failed_media"] == 1
    assert SystemNotification.select().where(
        SystemNotification.event_type == "media_invalid_detected"
    ).count() == 0


def test_backfill_retries_unknown_health(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="health-backfill",
        provider_key="demo",
        provider_config={},
    )
    unknown = _media(
        library,
        "BACKFILL-UNKNOWN",
        health={
            "status": "unknown",
            "reason": None,
            "detail": {"error_type": "provider_error"},
        },
    )
    probed = []

    def fake_probe_and_write(media, *, storage=None):
        probed.append(media.id)
        return PlaybackHealth(status=STATUS_OK), False

    monkeypatch.setattr(
        "src.service.playback.media_playback_health_backfill_service."
        "MEDIA_PROVIDER_REGISTRY.storage_for",
        lambda _library: object(),
    )
    monkeypatch.setattr(
        "src.service.playback.media_playback_health_backfill_service."
        "MediaPlaybackHealthService.probe_and_write",
        fake_probe_and_write,
    )

    stats = MediaPlaybackHealthBackfillService.backfill_pending_health(
        reporter=Reporter()
    )

    assert probed == [unknown.id]
    assert stats["candidate_media"] == 1
    assert stats["ok_media"] == 1
