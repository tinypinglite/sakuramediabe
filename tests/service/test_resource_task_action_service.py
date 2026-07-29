"""统一资源任务操作协议（Wave 4）护栏：
可执行性判定 / 部分成功 / 预算重开 / only_ids run 入队 / 连点 409。
"""

import pytest

from src.api.exception.errors import ApiError
from src.model import BackgroundTaskRun, Movie, ResourceTaskState
from src.service.system.resource_task_action_service import ResourceTaskActionService
from src.service.system.resource_task_actions_registry import available_actions_for_state

TASK_KEY = "movie_desc_translation"


def _movie(number: str) -> Movie:
    return Movie.create(movie_number=number, javdb_id=f"javdb-{number}", title=number)


def _state(movie: Movie, *, state: str, attempt_count: int = 3, retry_round: int = 0):
    return ResourceTaskState.create(
        task_key=TASK_KEY,
        resource_type="movie",
        resource_id=movie.id,
        state=state,
        attempt_count=attempt_count,
        retry_round=retry_round,
        error_code="translation_rejected",
        last_error="上游拒绝",
    )


def test_available_actions_follow_state_matrix(test_db):
    assert available_actions_for_state("failed_retryable") == [
        "retry_now",
        "rerun",
        "reset_retry_budget",
    ]
    assert available_actions_for_state("exhausted") == [
        "retry_now",
        "rerun",
        "reset_retry_budget",
    ]
    assert available_actions_for_state("failed_terminal") == ["rerun", "reset_retry_budget"]
    assert available_actions_for_state("succeeded") == ["rerun"]
    assert available_actions_for_state("running") == []
    assert available_actions_for_state("pending") == []


def test_retry_now_enqueues_subset_run_with_partial_success(test_db):
    retryable = _movie("ACT-001")
    _state(retryable, state="failed_retryable")
    exhausted = _movie("ACT-002")
    _state(exhausted, state="exhausted", retry_round=1)
    running = _movie("ACT-003")
    _state(running, state="running")
    missing_id = running.id + 10_000

    outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY,
        action="retry_now",
        resource_ids=[retryable.id, exhausted.id, running.id, missing_id],
    )

    assert outcome.accepted_resource_ids == [retryable.id, exhausted.id]
    assert [item["reason"] for item in outcome.skipped] == [
        "state_not_actionable",
        "task_state_not_found",
    ]
    # exhausted 隐式重开预算：轮次 +1、本轮计数归零、立即到期。
    refreshed = ResourceTaskState.get(ResourceTaskState.resource_id == exhausted.id)
    assert refreshed.state == "failed_retryable"
    assert refreshed.retry_round == 2
    assert refreshed.attempt_count == 0
    assert refreshed.next_retry_at is not None
    # 入队了带 only_ids 的可跟踪 run。
    task_run = BackgroundTaskRun.get_by_id(outcome.task_run_id)
    assert task_run.state == "pending"
    assert task_run.scheduled_at is not None
    assert task_run.params == {"only_ids": [retryable.id, exhausted.id]}
    assert task_run.mutex_key == f"resource_action:{TASK_KEY}"


def test_repeated_action_conflicts_with_409(test_db):
    movie = _movie("ACT-010")
    _state(movie, state="failed_retryable")
    first = ResourceTaskActionService.apply(
        task_key=TASK_KEY, action="retry_now", resource_ids=[movie.id]
    )
    assert first.task_run_id is not None

    ResourceTaskState.update(state="failed_retryable").where(
        ResourceTaskState.resource_id == movie.id
    ).execute()
    with pytest.raises(ApiError) as exc_info:
        ResourceTaskActionService.apply(
            task_key=TASK_KEY, action="retry_now", resource_ids=[movie.id]
        )
    assert exc_info.value.status_code == 409


def test_reset_retry_budget_reopens_without_run(test_db):
    movie = _movie("ACT-020")
    _state(movie, state="failed_terminal", attempt_count=5, retry_round=0)

    outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY, action="reset_retry_budget", resource_ids=[movie.id]
    )

    assert outcome.task_run_id is None
    refreshed = ResourceTaskState.get(ResourceTaskState.resource_id == movie.id)
    assert refreshed.state == "pending"
    assert refreshed.attempt_count == 0
    assert refreshed.retry_round == 1
    assert refreshed.error_code is None
    assert BackgroundTaskRun.select().count() == 0


def test_unknown_task_or_action_rejected(test_db):
    movie = _movie("ACT-030")
    _state(movie, state="failed_retryable")
    with pytest.raises(ApiError) as exc_info:
        ResourceTaskActionService.apply(
            task_key="ghost_task", action="retry_now", resource_ids=[movie.id]
        )
    assert exc_info.value.status_code == 404
    with pytest.raises(ApiError) as exc_info:
        ResourceTaskActionService.apply(
            task_key=TASK_KEY, action="explode", resource_ids=[movie.id]
        )
    assert exc_info.value.status_code == 422
