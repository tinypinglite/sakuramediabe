from datetime import datetime
import pytest

from src.api.exception.errors import ApiError
from src.model import ResourceTaskState
from src.service.system import ActivityService
from src.service.system.resource_task_state_service import ResourceTaskStateService


def test_get_state_or_default_returns_pending_snapshot_without_row(test_db):
    test_db.bind([ResourceTaskState], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([ResourceTaskState])

    snapshot = ResourceTaskStateService.get_state_or_default("movie_desc_sync", 11)

    assert snapshot.task_key == "movie_desc_sync"
    assert snapshot.resource_type == "movie"
    assert snapshot.resource_id == 11
    assert snapshot.state == ResourceTaskStateService.STATE_PENDING
    assert snapshot.attempt_count == 0


def test_mark_started_increments_attempt_count_and_clears_error(test_db):
    test_db.bind([ResourceTaskState], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([ResourceTaskState])
    record = ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=12,
        state=ResourceTaskStateService.STATE_FAILED,
        attempt_count=2,
        last_error="desc_not_found",
    )

    ResourceTaskStateService.mark_started("movie_desc_sync", 12, trigger_type="manual")
    refreshed = ResourceTaskState.get_by_id(record.id)

    assert refreshed.state == ResourceTaskStateService.STATE_RUNNING
    assert refreshed.attempt_count == 3
    assert refreshed.last_attempted_at is not None
    assert refreshed.last_error is None
    assert refreshed.last_trigger_type == "manual"


def test_mark_failed_sets_error_and_error_time(test_db):
    test_db.bind([ResourceTaskState], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([ResourceTaskState])

    ResourceTaskStateService.mark_failed(
        "movie_desc_sync",
        13,
        "desc_not_found",
        trigger_type="internal",
    )
    record = ResourceTaskState.get(
        ResourceTaskState.task_key == "movie_desc_sync",
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == 13,
    )

    assert record.state == ResourceTaskStateService.STATE_FAILED
    assert record.last_error == "desc_not_found"
    assert record.last_error_at is not None
    assert record.last_trigger_type == "internal"


def test_recover_running_records_only_updates_running_rows(test_db):
    test_db.bind([ResourceTaskState], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([ResourceTaskState])
    running_record = ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=21,
        state=ResourceTaskStateService.STATE_RUNNING,
        attempt_count=2,
    )
    pending_record = ResourceTaskState.create(
        task_key="movie_desc_sync",
        resource_type="movie",
        resource_id=22,
        state=ResourceTaskStateService.STATE_PENDING,
        last_error="pending_error",
    )

    recovered = ResourceTaskStateService.recover_running_records(
        "movie_desc_sync",
        "影片描述抓取任务中断，等待重试",
    )
    refreshed_running = ResourceTaskState.get_by_id(running_record.id)
    refreshed_pending = ResourceTaskState.get_by_id(pending_record.id)

    assert recovered == 1
    assert refreshed_running.state == ResourceTaskStateService.STATE_FAILED
    assert refreshed_running.last_error == "影片描述抓取任务中断，等待重试"
    assert refreshed_running.attempt_count == 2
    assert refreshed_pending.state == ResourceTaskStateService.STATE_PENDING
    assert refreshed_pending.last_error == "pending_error"


def test_list_definitions_includes_render_metadata():
    definitions = ResourceTaskStateService.list_definitions()
    definition_by_key = {definition.task_key: definition for definition in definitions}

    assert "movie_desc_sync" in definition_by_key
    assert definition_by_key["movie_desc_sync"].display_name == "影片描述回填"
    assert definition_by_key["media_thumbnail_generation"].resource_type == "media"
    assert definition_by_key["movie_desc_translation"].default_sort == "last_attempted_at:desc"
    assert definition_by_key["movie_title_translation"].display_name == "影片标题翻译"


def test_mark_started_uses_task_run_context_defaults(test_db):
    test_db.bind([ResourceTaskState], bind_refs=False, bind_backrefs=False)
    test_db.create_tables([ResourceTaskState])

    context_token = ActivityService.set_task_run_context(
        task_key="movie_desc_sync",
        task_run_id=77,
        trigger_type="scheduled",
    )
    try:
        ResourceTaskStateService.mark_started("movie_desc_sync", 41)
    finally:
        ActivityService.reset_task_run_context(context_token)

    record = ResourceTaskState.get(
        ResourceTaskState.task_key == "movie_desc_sync",
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == 41,
    )
    assert record.state == ResourceTaskStateService.STATE_RUNNING
    assert record.last_trigger_type == "scheduled"
    assert record.last_task_run_id == 77
