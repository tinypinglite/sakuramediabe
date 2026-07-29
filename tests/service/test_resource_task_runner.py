"""资源任务执行内核（Wave 2）生命周期护栏：
succeeded / failed_retryable+退避 / failed_terminal / exhausted / deferred / abort。
"""

from datetime import timedelta

import pytest

from src.common.runtime_time import utc_now_for_db
from src.model import Movie, ResourceTaskAttempt, ResourceTaskState
from src.service.system.resource_task_runner import (
    ResourceTaskLedger,
    ResourceTaskRunner,
    ResourceTaskSpec,
    RetryPolicy,
    TaskAbortError,
    TaskItemDeferred,
    TaskItemError,
)

TASK_KEY = "kernel_probe"


class StubReporter:
    def __init__(self):
        self.events = []

    def emit(self, **kwargs):
        self.events.append(kwargs)


def _create_movie(number: str) -> Movie:
    return Movie.create(movie_number=number, javdb_id=f"javdb-{number}", title=number)


def _build_spec(behavior: dict[int, Exception | None], *, max_attempts: int = 3) -> ResourceTaskSpec:
    def select_candidates(state_condition, only_ids=None):
        condition = state_condition(TASK_KEY, "movie", Movie.id)
        query = Movie.select().where(condition).order_by(Movie.id)
        if only_ids:
            query = query.where(Movie.id.in_(only_ids))
        return query

    def process_one(_ctx, movie):
        outcome = behavior.get(movie.id)
        if outcome is not None:
            raise outcome

    return ResourceTaskSpec(
        task_key=TASK_KEY,
        resource_type="movie",
        retry=RetryPolicy(max_attempts=max_attempts, backoff_base_seconds=600),
        select_candidates=select_candidates,
        process_one=process_one,
    )


def _record(movie: Movie) -> ResourceTaskState:
    return ResourceTaskState.get(
        ResourceTaskState.task_key == TASK_KEY,
        ResourceTaskState.resource_id == movie.id,
    )


def test_runner_success_writes_projection_and_attempt(test_db):
    movie = _create_movie("KER-001")

    stats = ResourceTaskRunner.run(_build_spec({}), StubReporter())

    assert stats["candidate_count"] == 1
    assert stats["succeeded_count"] == 1
    record = _record(movie)
    assert record.state == "succeeded"
    # 成功收口本轮：计数归零，终身次数由 attempt 表体现。
    assert record.attempt_count == 0
    assert record.next_retry_at is None
    attempt = ResourceTaskAttempt.get_by_id(record.last_attempt_id)
    assert attempt.state == "succeeded"
    assert attempt.attempt_no == 1


def test_runner_retryable_failure_sets_backoff_and_reenters_after_due(test_db):
    movie = _create_movie("KER-002")
    spec = _build_spec({movie.id: TaskItemError("probe_failed", "boom")})

    stats = ResourceTaskRunner.run(spec, StubReporter())

    assert stats["failed_retryable_count"] == 1
    record = _record(movie)
    assert record.state == "failed_retryable"
    assert record.error_code == "probe_failed"
    assert record.next_retry_at is not None and record.next_retry_at > utc_now_for_db()

    # 未到 next_retry_at：不进候选。
    assert ResourceTaskRunner.run(spec, StubReporter())["candidate_count"] == 0

    # 拨到期后重新进候选，attempt_no 递增。
    ResourceTaskState.update(next_retry_at=utc_now_for_db() - timedelta(seconds=1)).where(
        ResourceTaskState.id == record.id
    ).execute()
    stats = ResourceTaskRunner.run(spec, StubReporter())
    assert stats["candidate_count"] == 1
    record = _record(movie)
    assert record.attempt_count == 2
    assert ResourceTaskAttempt.get_by_id(record.last_attempt_id).attempt_no == 2


def test_runner_terminal_failure_never_reenters(test_db):
    movie = _create_movie("KER-003")
    spec = _build_spec(
        {movie.id: TaskItemError("confirmed_missing", "确认无此番号", retryable=False)}
    )

    stats = ResourceTaskRunner.run(spec, StubReporter())

    assert stats["failed_terminal_count"] == 1
    record = _record(movie)
    assert record.state == "failed_terminal"
    assert record.next_retry_at is None
    assert ResourceTaskRunner.run(spec, StubReporter())["candidate_count"] == 0


def test_runner_exhausts_budget_after_max_attempts(test_db):
    movie = _create_movie("KER-004")
    spec = _build_spec({movie.id: TaskItemError("probe_failed")}, max_attempts=2)

    ResourceTaskRunner.run(spec, StubReporter())
    ResourceTaskState.update(next_retry_at=utc_now_for_db() - timedelta(seconds=1)).where(
        ResourceTaskState.resource_id == movie.id
    ).execute()
    stats = ResourceTaskRunner.run(spec, StubReporter())

    assert stats["exhausted_count"] == 1
    record = _record(movie)
    assert record.state == "exhausted"
    assert record.attempt_count == 2
    assert ResourceTaskRunner.run(spec, StubReporter())["candidate_count"] == 0


def test_runner_deferred_keeps_state_and_budget(test_db):
    movie = _create_movie("KER-005")
    spec = _build_spec({movie.id: TaskItemDeferred("source_unavailable")})

    stats = ResourceTaskRunner.run(spec, StubReporter())

    assert stats["deferred_count"] == 1
    record = _record(movie)
    assert record.state == "pending"
    assert record.attempt_count == 0
    attempt = ResourceTaskAttempt.get(ResourceTaskAttempt.resource_id == movie.id)
    assert attempt.state == "deferred"
    # 不耗预算：下一轮仍进候选。
    assert ResourceTaskRunner.run(_build_spec({}), StubReporter())["succeeded_count"] == 1


def test_runner_abort_rolls_back_current_and_reraises(test_db):
    first = _create_movie("KER-006")
    second = _create_movie("KER-007")
    spec = _build_spec({first.id: TaskAbortError("upstream_fused", "DMM 连续失败熔断")})

    with pytest.raises(TaskAbortError):
        ResourceTaskRunner.run(spec, StubReporter())

    first_record = _record(first)
    assert first_record.state == "pending"
    assert first_record.attempt_count == 0
    # 中止发生在第一个资源：后面的资源保持无状态行，等下一轮。
    assert (
        ResourceTaskState.select()
        .where(ResourceTaskState.resource_id == second.id)
        .count()
        == 0
    )


def test_runner_only_ids_limits_candidates(test_db):
    first = _create_movie("KER-008")
    second = _create_movie("KER-009")

    stats = ResourceTaskRunner.run(
        _build_spec({}), StubReporter(), only_ids=[second.id]
    )

    assert stats["candidate_count"] == 1
    assert _record(second).state == "succeeded"
    assert (
        ResourceTaskState.select()
        .where(ResourceTaskState.resource_id == first.id)
        .count()
        == 0
    )


def _forced_spec(movie_ids: list[int], *, claim_eligible=None) -> ResourceTaskSpec:
    """无视内核状态条件的候选查询：模拟批跑候选快照过期后仍持有该资源的场景。"""

    def select_candidates(_state_condition, only_ids=None):
        return list(Movie.select().where(Movie.id.in_(movie_ids)).order_by(Movie.id))

    return ResourceTaskSpec(
        task_key=TASK_KEY,
        resource_type="movie",
        retry=RetryPolicy(max_attempts=3, backoff_base_seconds=600),
        select_candidates=select_candidates,
        process_one=lambda _ctx, _movie: None,
        claim_eligible=claim_eligible,
    )


def _create_state(movie: Movie, state: str) -> ResourceTaskState:
    return ResourceTaskState.create(
        task_key=TASK_KEY, resource_type="movie", resource_id=movie.id, state=state
    )


def test_runner_claim_refuses_running_row(test_db):
    # 行级领取：running 行说明有其它 run 正在处理，跳过且不写 attempt、不覆盖投影。
    movie = _create_movie("KER-010")
    _create_state(movie, "running")

    stats = ResourceTaskRunner.run(_forced_spec([movie.id]), StubReporter())

    assert stats["skipped_count"] == 1
    assert stats["succeeded_count"] == 0
    assert _record(movie).state == "running"
    assert (
        ResourceTaskAttempt.select()
        .where(ResourceTaskAttempt.resource_id == movie.id)
        .count()
        == 0
    )


def test_runner_default_claim_recheck_skips_stale_snapshot(test_db):
    # 快照后资源已被子集跑做成 succeeded：默认领取复核在行锁内识别并跳过，不重复处理。
    movie = _create_movie("KER-011")
    _create_state(movie, "succeeded")

    stats = ResourceTaskRunner.run(_forced_spec([movie.id]), StubReporter())

    assert stats["skipped_count"] == 1
    assert _record(movie).state == "succeeded"


def test_runner_resync_claim_allows_succeeded_row(test_db):
    # 互动同步/订阅搜索语义：succeeded 行本来就是候选，宽松复核放行重采。
    movie = _create_movie("KER-012")
    _create_state(movie, "succeeded")

    stats = ResourceTaskRunner.run(
        _forced_spec([movie.id], claim_eligible=ResourceTaskLedger.resync_claim_eligible),
        StubReporter(),
    )

    assert stats["succeeded_count"] == 1
    assert _record(movie).state == "succeeded"


def test_ledger_force_claim_only_refuses_running(test_db):
    # 单资源直跑（不传 eligible_check）：succeeded 可强制领取（重跑语义），running 拒绝。
    movie = _create_movie("KER-013")
    _create_state(movie, "succeeded")

    claim = ResourceTaskLedger.begin_attempt(
        task_key=TASK_KEY,
        resource_type="movie",
        resource_id=movie.id,
        trigger_type="manual",
        task_run_id=None,
    )
    assert claim is not None
    attempt, record, prior_state = claim
    assert prior_state == "succeeded"
    ResourceTaskLedger.finish_success(attempt, record)

    ResourceTaskState.update(state="running").where(
        ResourceTaskState.resource_id == movie.id
    ).execute()
    assert (
        ResourceTaskLedger.begin_attempt(
            task_key=TASK_KEY,
            resource_type="movie",
            resource_id=movie.id,
            trigger_type="manual",
            task_run_id=None,
        )
        is None
    )
