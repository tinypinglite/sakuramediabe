from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace

import pytest

from src.plugins.provider_protocol import ThumbnailArtifact, ThumbnailGeneration
from src.service.playback.thumbnails import progress as progress_module
from src.service.playback.thumbnails import task_service
from src.service.playback.thumbnails.progress import ThumbnailTaskProgress
from src.service.playback.thumbnails.task_service import (
    MediaThumbnailTaskService,
    ThumbnailGenerationOutcome,
)


@pytest.mark.parametrize("fail", [False, True])
def test_thumbnail_progress_during_scan_and_first_media(monkeypatch, fail):
    events = []
    scan_heartbeat = Event()
    media_heartbeat = Event()
    monkeypatch.setattr(ThumbnailTaskProgress, "INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(
        progress_module, "get_database",
        lambda: SimpleNamespace(connection_context=nullcontext),
    )

    def emit(**payload):
        events.append(payload)
        if "本步骤已等待" in payload["text"]:
            if "查询候选" in payload["text"]:
                scan_heartbeat.set()
            if "已生成 1/3 张" in payload["text"]:
                media_heartbeat.set()

    def candidates():
        assert "查询候选" in events[0]["text"]
        assert scan_heartbeat.wait(2)
        return [(12, ("local", 1))]

    def generate(media_id, progress_callback):
        assert media_id == 12
        assert "正在准备视频" in events[-1]["text"]
        progress_callback("正在生成缩略图 · 已生成 1/3 张")
        assert media_heartbeat.wait(2)
        assert events[-1]["current"] == 0
        assert events[-1]["total"] == 1
        if fail:
            raise RuntimeError("task interrupted")
        return ThumbnailGenerationOutcome("succeeded", generated_count=3)

    monkeypatch.setattr(MediaThumbnailTaskService, "_candidate_entries", candidates)
    monkeypatch.setattr(MediaThumbnailTaskService, "_generate_one", generate)
    reporter = SimpleNamespace(emit=emit)
    if fail:
        with pytest.raises(RuntimeError, match="task interrupted"):
            MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)
    else:
        MediaThumbnailTaskService.generate_pending_thumbnails(reporter=reporter)
        assert events[-1]["current"] == events[-1]["total"] == 1
        assert "任务完成" in events[-1]["text"]
        assert "成功 1" in events[-1]["text"]
    # After return (including exceptions), no heartbeat may overwrite the final state.
    count = len(events)
    Event().wait(0.08)
    assert len(events) == count


def test_thumbnail_empty_task_finishes_explicitly(monkeypatch):
    events = []
    monkeypatch.setattr(MediaThumbnailTaskService, "_candidate_entries", list)
    result = MediaThumbnailTaskService.generate_pending_thumbnails(
        reporter=SimpleNamespace(emit=lambda **payload: events.append(payload)),
    )
    assert result["pending_media"] == 0
    assert events[0]["current"] == events[0]["total"] == 0
    assert "无待处理媒体" in events[-1]["text"]


@pytest.mark.parametrize("supports_progress", [False, True])
def test_thumbnail_provider_progress_and_installed_legacy_provider(monkeypatch, supports_progress):
    events = []
    generation = ThumbnailGeneration(1, (ThumbnailArtifact(0, "0.webp"),))

    def legacy(*, media, workspace):
        return generation

    def modern(*, media, workspace, progress_callback=None):
        progress_callback("已生成 1/1 张")
        return generation

    monkeypatch.setattr(task_service, "media_handle_for", lambda media: media)
    monkeypatch.setattr(
        task_service.MEDIA_PROVIDER_REGISTRY, "storage_for",
        lambda _library: SimpleNamespace(generate_thumbnails=modern if supports_progress else legacy),
    )
    monkeypatch.setattr(
        task_service.ThumbnailArtifactService, "validate_artifact",
        lambda workspace, artifact: workspace / artifact.relative_path,
    )
    monkeypatch.setattr(task_service.ThumbnailArtifactService, "persist", lambda media, artifacts: len(artifacts))
    result = MediaThumbnailTaskService._generate_artifacts(
        SimpleNamespace(id=1, library=None), events.append,
    )
    assert result == 1
    assert ("已生成 1/1 张" in events) == supports_progress
    assert events[-1] == "正在校验并保存缩略图"
