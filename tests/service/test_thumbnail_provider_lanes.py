from types import SimpleNamespace

from src.service.playback.thumbnails.task_service import (
    MediaThumbnailTaskService,
    ThumbnailGenerationOutcome,
)


def test_thumbnail_backend_failure_pauses_only_its_library(monkeypatch):
    calls = []
    events = []
    monkeypatch.setattr(
        MediaThumbnailTaskService,
        "_candidate_entries",
        lambda: [(1, ("cloud115", 1)), (2, ("cloud115", 1)), (3, ("local", 2))],
    )

    def generate(media_id):
        calls.append(media_id)
        if media_id == 1:
            return ThumbnailGenerationOutcome(
                "backend_unavailable", error_code="cloud115_thumbnail_unavailable"
            )
        return ThumbnailGenerationOutcome("succeeded", generated_count=2)

    monkeypatch.setattr(MediaThumbnailTaskService, "_generate_one", generate)

    result = MediaThumbnailTaskService.generate_pending_thumbnails(
        reporter=SimpleNamespace(emit=lambda **kwargs: events.append(kwargs))
    )

    assert calls == [1, 3]
    assert result["backend_failed_lanes"] == 1
    assert result["backend_deferred_media"] == 2
    assert result["successful_media"] == 1
    assert result["generated_thumbnails"] == 2
    assert len(events) == 3
