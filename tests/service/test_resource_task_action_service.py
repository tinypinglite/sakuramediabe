"""统一资源任务操作协议（Wave 4）护栏：
可执行性判定 / 领域合格性钩子 / 部分成功 / 预算重开 / 无行播种 /
批量按状态圈定 / only_ids run 入队 / mutex 粒度与连点 409。
"""

import pytest

from src.api.exception.errors import ApiError
from src.model import BackgroundTaskRun, Movie, ResourceTaskState
from src.service.system.resource_task_action_service import ResourceTaskActionService
from src.service.system.resource_task_actions_registry import (
    available_actions_for_state,
)
from src.service.system.resource_task_state_service import ResourceTaskStateService

TASK_KEY = "movie_interaction_sync"


def _movie(
    number: str,
    *,
    desc: str = "原始简介",
    is_subscribed: bool = False,
    javdb_id: str | None = None,
) -> Movie:
    return Movie.create(
        movie_number=number,
        javdb_id=javdb_id if javdb_id is not None else f"javdb-{number}",
        title=number,
        desc=desc,
        is_subscribed=is_subscribed,
    )


def _state(
    movie: Movie,
    *,
    state: str,
    task_key: str = TASK_KEY,
    attempt_count: int = 3,
    retry_round: int = 0,
):
    return ResourceTaskState.create(
        task_key=task_key,
        resource_type="movie",
        resource_id=movie.id,
        state=state,
        attempt_count=attempt_count,
        retry_round=retry_round,
        error_code="interaction_sync_rejected",
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
    # rerun 是通用"立即强制执行"入口：pending（等 cron / 播种行）也可操作。
    assert available_actions_for_state("pending") == ["rerun"]
    # 任务声明支持集裁剪：不声明 rerun 的任务（缩略图 / 订阅查询）不出现 rerun。
    thumbnail_supported = ResourceTaskStateService.get_definition(
        "media_thumbnail_generation"
    ).supported_actions
    assert available_actions_for_state("succeeded", thumbnail_supported) == []
    assert available_actions_for_state("exhausted", thumbnail_supported) == [
        "retry_now",
        "reset_retry_budget",
    ]


def test_retry_now_enqueues_subset_run_with_partial_success(test_db):
    retryable = _movie("ACT-001")
    _state(retryable, state="failed_retryable")
    exhausted = _movie("ACT-002")
    _state(exhausted, state="exhausted", retry_round=1)
    running = _movie("ACT-003")
    _state(running, state="running")
    missing_movie = _movie("ACT-004")
    missing_movie_id = missing_movie.id
    Movie.delete().where(Movie.id == missing_movie_id).execute()

    outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY,
        action="retry_now",
        resource_ids=[retryable.id, exhausted.id, running.id, missing_movie_id],
    )

    assert outcome.accepted_resource_ids == [retryable.id, exhausted.id]
    assert [item["reason"] for item in outcome.skipped] == [
        "state_not_actionable",
        # 合格性钩子先于状态判定：影片已不存在报 movie_not_found。
        "movie_not_found",
    ]
    # exhausted 隐式重开预算：轮次 +1、本轮计数归零、立即到期。
    refreshed = ResourceTaskState.get(ResourceTaskState.resource_id == exhausted.id)
    assert refreshed.state == "failed_retryable"
    assert refreshed.retry_round == 2
    assert refreshed.attempt_count == 0
    assert refreshed.next_retry_at is not None
    # 入队了带 only_ids 的可跟踪 run；多资源操作互斥在任务粒度。
    task_run = BackgroundTaskRun.get_by_id(outcome.task_run_id)
    assert task_run.state == "pending"
    assert task_run.scheduled_at is not None
    assert task_run.params == {"only_ids": [retryable.id, exhausted.id]}
    assert task_run.mutex_key == f"resource_action:{TASK_KEY}"


def test_domain_eligibility_hook_skips_before_state_transition(test_db):
    # movie_interaction_sync 的合格性钩子要求 javdb_id 非空。
    no_javdb = _movie("ACT-005", javdb_id="")
    _state(no_javdb, state="failed_retryable")

    outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY, action="retry_now", resource_ids=[no_javdb.id]
    )

    assert outcome.accepted_resource_ids == []
    assert outcome.skipped == [
        {"resource_id": no_javdb.id, "reason": "movie_javdb_id_missing"}
    ]
    assert outcome.task_run_id is None
    # 状态未被动过。
    untouched = ResourceTaskState.get(ResourceTaskState.resource_id == no_javdb.id)
    assert untouched.state == "failed_retryable"


def test_rerun_seeds_missing_record_and_forces_run(test_db):
    # 从未记账的影片也能 rerun：就地播种 pending 行并入队 only_ids run。
    fresh = _movie("ACT-006")

    outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY, action="rerun", resource_ids=[fresh.id]
    )

    assert outcome.accepted_resource_ids == [fresh.id]
    seeded = ResourceTaskState.get(
        ResourceTaskState.task_key == TASK_KEY,
        ResourceTaskState.resource_id == fresh.id,
    )
    assert seeded.state == "pending"
    assert seeded.retry_round == 1
    assert seeded.last_trigger_type == "manual"
    task_run = BackgroundTaskRun.get_by_id(outcome.task_run_id)
    assert task_run.params == {"only_ids": [fresh.id]}
    # 单资源操作互斥细化到资源粒度。
    assert task_run.mutex_key == f"resource_action:{TASK_KEY}:{fresh.id}"


def test_retry_now_and_reset_do_not_seed_missing_record(test_db):
    fresh = _movie("ACT-007")

    outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY, action="retry_now", resource_ids=[fresh.id]
    )

    assert outcome.accepted_resource_ids == []
    assert outcome.skipped == [
        {"resource_id": fresh.id, "reason": "task_state_not_found"}
    ]
    assert (
        ResourceTaskState.select()
        .where(ResourceTaskState.resource_id == fresh.id)
        .count()
        == 0
    )


def test_single_resource_mutex_isolates_between_resources(test_db):
    first = _movie("ACT-008")
    _state(first, state="failed_retryable")
    second = _movie("ACT-009")
    _state(second, state="failed_retryable")

    first_outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY, action="retry_now", resource_ids=[first.id]
    )
    # 不同影片的单资源操作互不阻塞（资源粒度 mutex）。
    second_outcome = ResourceTaskActionService.apply(
        task_key=TASK_KEY, action="retry_now", resource_ids=[second.id]
    )
    assert first_outcome.task_run_id != second_outcome.task_run_id

    # 同一影片连点：状态拨回可操作后仍被资源粒度 mutex 顶 409。
    ResourceTaskState.update(state="failed_retryable").where(
        ResourceTaskState.resource_id == first.id
    ).execute()
    with pytest.raises(ApiError) as exc_info:
        ResourceTaskActionService.apply(
            task_key=TASK_KEY, action="retry_now", resource_ids=[first.id]
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


def test_bulk_reset_by_state_with_domain_hook(test_db):
    # 批量圈定 = 旧订阅页 reset_all_exhausted 的对等物：未订阅影片由钩子跳过。
    task_key = "subscribed_movie_auto_download"
    subscribed = _movie("ACT-030", is_subscribed=True)
    _state(subscribed, state="exhausted", task_key=task_key, retry_round=2)
    unsubscribed = _movie("ACT-031", is_subscribed=False)
    _state(unsubscribed, state="exhausted", task_key=task_key)
    retryable = _movie("ACT-032", is_subscribed=True)
    _state(retryable, state="failed_retryable", task_key=task_key)

    outcome = ResourceTaskActionService.apply(
        task_key=task_key, action="reset_retry_budget", state="exhausted"
    )

    assert outcome.accepted_resource_ids == [subscribed.id]
    assert outcome.skipped == [
        {"resource_id": unsubscribed.id, "reason": "movie_not_subscribed"}
    ]
    assert outcome.task_run_id is None
    refreshed = ResourceTaskState.get(ResourceTaskState.resource_id == subscribed.id)
    assert refreshed.state == "pending"
    assert refreshed.retry_round == 3
    assert refreshed.attempt_count == 0
    # 状态筛选之外的行不受影响。
    untouched = ResourceTaskState.get(ResourceTaskState.resource_id == retryable.id)
    assert untouched.state == "failed_retryable"


def test_bulk_selection_rejected_for_run_actions(test_db):
    with pytest.raises(ApiError) as exc_info:
        ResourceTaskActionService.apply(task_key=TASK_KEY, action="retry_now")
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "empty_resource_ids"


def test_action_not_supported_by_task_rejected(test_db):
    # 订阅查询不开放 rerun：强制重搜会绕过"已有媒体/活跃下载"防护。
    movie = _movie("ACT-040", is_subscribed=True)
    _state(movie, state="exhausted", task_key="subscribed_movie_auto_download")
    with pytest.raises(ApiError) as exc_info:
        ResourceTaskActionService.apply(
            task_key="subscribed_movie_auto_download",
            action="rerun",
            resource_ids=[movie.id],
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "resource_task_action_not_supported"


def test_unknown_task_or_action_rejected(test_db):
    movie = _movie("ACT-050")
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
