from types import SimpleNamespace

import pytest

from src.api.exception.errors import ApiError
from src.model import BackgroundTaskRun, MediaLibrary
from src.schema.transfers.media_import import ImportMetadataSearchResponse
from src.service.transfers.shared.import_task_service import ImportTaskService


def _failure_item(library_id):
    return {
        "id": "failure-1",
        "path": "release/no-number.mp4",
        "name": "no-number.mp4",
        "relative_path": "release/no-number.mp4",
        "size_bytes": 100,
        "is_video": True,
        "reason": "movie_number_not_found",
        "detail": "无法从文件名识别番号",
        "kind": "file",
        "source_ref": {"file": "opaque-file-ref"},
        "library_id": library_id,
        "media_kind": "jav",
        "source_disposition": "keep",
        "state": "pending",
        "retry_task_run_id": None,
        "resolved_movie_id": None,
        "resolved_media_id": None,
        "last_retry_error": None,
    }


def _import_task(library_id):
    item = _failure_item(library_id)
    task = BackgroundTaskRun.create(
        task_key=ImportTaskService.TASK_KEY,
        task_name="JAV媒体库导入",
        trigger_type="manual",
        state="failed",
        result_summary={"failed_files": [item]},
    )
    return task, item


def test_failed_items_are_readable_without_exposing_provider_source_ref(test_db):
    library = MediaLibrary.create(
        name="failed-items-library", provider_key="test", provider_config={}
    )
    task, _item = _import_task(library.id)

    resources = ImportTaskService.list_failed_items(task.id)

    assert len(resources) == 1
    payload = resources[0].model_dump()
    assert payload["id"] == "failure-1"
    assert payload["can_manual_search"] is True
    assert "source_ref" not in payload


def test_skipped_items_are_listed_but_not_searchable(test_db):
    library = MediaLibrary.create(
        name="skipped-items-library", provider_key="test", provider_config={}
    )
    task, item = _import_task(library.id)
    item.update({"reason": "file_too_small", "kind": "skipped"})
    task.result_summary = {"failed_files": [item]}
    task.save(only=[BackgroundTaskRun.result_summary])

    resources = ImportTaskService.list_failed_items(task.id)

    assert len(resources) == 1
    assert resources[0].can_manual_search is False
    with pytest.raises(ApiError) as exc_info:
        ImportTaskService.search_failed_item(task.id, item["id"], "ABC-001")
    assert exc_info.value.code == "failed_item_search_unavailable"


def test_failed_item_search_uses_only_the_requested_number(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="failed-search-library", provider_key="test", provider_config={}
    )
    task, _item = _import_task(library.id)
    seen = []
    expected = ImportMetadataSearchResponse(movie_number="ABC-001")
    monkeypatch.setattr(
        "src.service.catalog.movie_metadata_search_service.MovieMetadataSearchService.search_by_number",
        lambda movie_number: (seen.append(movie_number) or expected),
    )

    result = ImportTaskService.search_failed_item(task.id, "failure-1", "ABC-001")

    assert result is expected
    assert seen == ["ABC-001"]


def test_retry_is_enqueued_and_worker_marks_original_item_resolved(test_db, monkeypatch):
    library = MediaLibrary.create(
        name="failed-retry-library", provider_key="test", provider_config={}
    )
    task, _item = _import_task(library.id)
    retry_calls = []

    def retry_failed_file(_service, failure_item, candidate_id, *, operation_key):
        retry_calls.append((failure_item, candidate_id, operation_key))
        return {
            "imported_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "new_playable_movies": [],
            "created_video_ids": [33],
            "movie_id": 11,
            "media_id": 22,
        }

    monkeypatch.setattr(
        "src.service.transfers.imports.import_service.MediaImportService.retry_failed_file",
        retry_failed_file,
    )

    accepted = ImportTaskService.enqueue_failed_item_retry(
        task.id,
        "failure-1",
        "javdb:ABC-001:javdb-001",
    )

    retry_task = BackgroundTaskRun.get_by_id(accepted.task_run_id)
    stored_original = BackgroundTaskRun.get_by_id(task.id)
    assert retry_task.params["mode"] == "retry_failed_file"
    assert accepted.task_key == ImportTaskService.TASK_KEY
    assert accepted.state == "pending"
    assert retry_calls == []
    assert retry_task.params["failure_item"]["source_ref"] == {
        "file": "opaque-file-ref"
    }
    assert stored_original.result_summary["failed_files"][0]["state"] == "queued"

    reporter = SimpleNamespace(
        task_run_id=retry_task.id,
        emit=lambda **_kwargs: None,
    )

    result = ImportTaskService.execute(reporter, retry_task.params)

    assert result["movie_id"] == 11
    assert len(retry_calls) == 1
    assert retry_calls[0][0]["source_ref"] == {"file": "opaque-file-ref"}
    assert retry_calls[0][1] == "javdb:ABC-001:javdb-001"
    assert retry_calls[0][2].startswith(f"task:{retry_task.id}:")
    stored_original = BackgroundTaskRun.get_by_id(task.id)
    resolved = stored_original.result_summary["failed_files"][0]
    assert resolved["state"] == "resolved"
    assert resolved["resolved_movie_id"] == 11
    assert resolved["resolved_media_id"] == 22


def _set_failure_item_state(task, item_id, state, retry_task_run_id):
    for item in task.result_summary["failed_files"]:
        if item["id"] == item_id:
            item["state"] = state
            item["retry_task_run_id"] = retry_task_run_id
    task.save(only=[BackgroundTaskRun.result_summary])


def _create_retry_run(task, item_id, *, state, error_message=None):
    return BackgroundTaskRun.create(
        task_key=ImportTaskService.TASK_KEY,
        task_name="JAV失败项重试导入",
        trigger_type="manual",
        state=state,
        error_message=error_message,
        params={
            "mode": "retry_failed_file",
            "original_task_run_id": task.id,
            "failure_item_id": item_id,
        },
    )


def test_recovery_restores_only_items_queued_for_the_interrupted_retry(test_db):
    library = MediaLibrary.create(
        name="failed-retry-recovery-library", provider_key="test", provider_config={}
    )
    task, item = _import_task(library.id)
    other_item = {**item, "id": "failure-2"}
    task.result_summary = {"failed_files": [item, other_item]}
    task.save(only=[BackgroundTaskRun.result_summary])
    interrupted_retry = _create_retry_run(
        task,
        item["id"],
        state="failed",
        error_message="任务执行进程重启，执行已中断",
    )
    active_retry = _create_retry_run(task, other_item["id"], state="pending")
    _set_failure_item_state(task, item["id"], "queued", interrupted_retry.id)
    _set_failure_item_state(task, other_item["id"], "queued", active_retry.id)

    ImportTaskService.recover_interrupted_downloads()

    restored = {
        current["id"]: current
        for current in BackgroundTaskRun.get_by_id(task.id).result_summary["failed_files"]
    }
    assert restored[item["id"]]["state"] == "pending"
    assert restored[item["id"]]["last_retry_error"] == "任务执行进程重启，执行已中断"
    assert restored[other_item["id"]]["state"] == "queued"
