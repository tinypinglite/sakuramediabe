from datetime import datetime, timedelta

import pytest

from src.model import Movie, RankingItem, ResourceTaskState
from sakuramedia_metadata_providers.models import JavdbMovieDetailResource
from src.service.catalog.movie_interaction_sync_service import MovieInteractionSyncService
from src.service.system import ActivityService


def _build_detail(
    javdb_id: str,
    movie_number: str,
    *,
    score: float = 0,
    score_number: int = 0,
    watched_count: int = 0,
    want_watch_count: int = 0,
    comment_count: int = 0,
) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=javdb_id,
        movie_number=movie_number,
        title=movie_number,
        cover_image=None,
        release_date="2024-01-01",
        duration_minutes=120,
        score=score,
        watched_count=watched_count,
        want_watch_count=want_watch_count,
        comment_count=comment_count,
        score_number=score_number,
        is_subscribed=False,
        summary="",
        series_name=None,
        maker_name=None,
        director_name=None,
        actors=[],
        tags=[],
        extra=None,
        plot_images=[],
    )


class FakeProvider:
    def __init__(self, details=None, failures=None):
        self.details = details or {}
        self.failures = failures or {}
        self.calls = []

    def get_movie_by_javdb_id(self, javdb_id: str):
        self.calls.append(javdb_id)
        if javdb_id in self.failures:
            raise self.failures[javdb_id]
        return self.details[javdb_id]


def _create_movie(movie_number: str, javdb_id: str, **kwargs) -> Movie:
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": movie_number,
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def _create_task_state(movie: Movie, **kwargs) -> ResourceTaskState:
    payload = {
        "task_key": MovieInteractionSyncService.TASK_KEY,
        "resource_type": "movie",
        "resource_id": movie.id,
    }
    payload.update(kwargs)
    return ResourceTaskState.create(**payload)


def _get_task_state(movie_id: int) -> ResourceTaskState:
    return ResourceTaskState.get(
        ResourceTaskState.task_key == MovieInteractionSyncService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie_id,
    )


def _create_ranking_item(
    movie: Movie,
    *,
    source_key: str = "javdb",
    board_key: str = "censored",
    period: str = "daily",
    rank: int = 1,
) -> RankingItem:
    return RankingItem.create(
        source_key=source_key,
        board_key=board_key,
        period=period,
        rank=rank,
        movie_number=movie.movie_number,
        movie=movie,
    )


def _collect_candidate_movie_numbers(service: MovieInteractionSyncService, *, now: datetime) -> list[str]:
    service._now = lambda: now
    return [movie.movie_number for movie in service._collect_candidates(now=now)]


def test_movie_interaction_sync_service_selects_due_movies_by_subscription_and_release_window(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    subscribed_due = _create_movie(
        "SUB-001",
        "sub-1",
        release_date=now - timedelta(days=300),
        is_subscribed=True,
    )
    _create_task_state(subscribed_due, state="succeeded", last_succeeded_at=now - timedelta(days=1, minutes=1))
    future_due = _create_movie(
        "FUT-001",
        "future-1",
        release_date=now + timedelta(days=7),
    )
    _create_task_state(future_due, state="succeeded", last_succeeded_at=now - timedelta(days=1))
    recent_due = _create_movie(
        "REC-001",
        "recent-1",
        release_date=now - timedelta(days=30),
    )
    _create_task_state(recent_due, state="succeeded", last_succeeded_at=now - timedelta(days=1))
    middle_due = _create_movie(
        "MID-001",
        "middle-1",
        release_date=now - timedelta(days=90),
    )
    _create_task_state(middle_due, state="succeeded", last_succeeded_at=now - timedelta(days=3))
    old_due = _create_movie(
        "OLD-001",
        "old-1",
        release_date=now - timedelta(days=365),
    )
    _create_task_state(old_due, state="succeeded", last_succeeded_at=now - timedelta(days=7))
    null_release_due = _create_movie(
        "NULL-001",
        "null-1",
        release_date=None,
    )
    _create_task_state(null_release_due, state="succeeded", last_succeeded_at=now - timedelta(days=7))
    unsynced_due = _create_movie(
        "NEW-001",
        "new-1",
        release_date=now - timedelta(days=2),
    )
    _create_movie(
        "REC-002",
        "recent-2",
        release_date=now - timedelta(days=10),
    )
    _create_task_state(Movie.get(Movie.movie_number == "REC-002"), state="succeeded", last_succeeded_at=now - timedelta(hours=12))
    _create_movie(
        "MID-002",
        "middle-2",
        release_date=now - timedelta(days=100),
    )
    _create_task_state(Movie.get(Movie.movie_number == "MID-002"), state="succeeded", last_succeeded_at=now - timedelta(days=2, hours=23))
    _create_movie(
        "OLD-002",
        "old-2",
        release_date=now - timedelta(days=365),
    )
    _create_task_state(Movie.get(Movie.movie_number == "OLD-002"), state="succeeded", last_succeeded_at=now - timedelta(days=6, hours=23))

    provider = FakeProvider(
        details={
            "sub-1": _build_detail("sub-1", subscribed_due.movie_number),
            "future-1": _build_detail("future-1", future_due.movie_number),
            "recent-1": _build_detail("recent-1", recent_due.movie_number),
            "middle-1": _build_detail("middle-1", middle_due.movie_number),
            "old-1": _build_detail("old-1", old_due.movie_number),
            "null-1": _build_detail("null-1", null_release_due.movie_number),
            "new-1": _build_detail("new-1", unsynced_due.movie_number),
        }
    )
    service = MovieInteractionSyncService(provider=provider)
    service._now = lambda: now

    stats = service.run()

    assert stats["candidate_movies"] == 7
    assert stats["processed_movies"] == 7
    assert stats["succeeded_movies"] == 7
    assert stats["failed_movies"] == 0
    assert set(provider.calls) == {"sub-1", "future-1", "recent-1", "middle-1", "old-1", "null-1", "new-1"}


def test_movie_interaction_sync_service_refreshes_ranked_movies_every_hour(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    ranked_due = _create_movie(
        "RANK-001",
        "rank-1",
        release_date=now - timedelta(days=365),
    )
    ranked_not_due = _create_movie(
        "RANK-002",
        "rank-2",
        release_date=now - timedelta(days=365),
    )
    ordinary_old = _create_movie(
        "OLD-003",
        "old-3",
        release_date=now - timedelta(days=365),
    )
    _create_task_state(ranked_due, state="succeeded", last_succeeded_at=now - timedelta(hours=1, minutes=1))
    _create_task_state(ranked_not_due, state="succeeded", last_succeeded_at=now - timedelta(minutes=59))
    _create_task_state(ordinary_old, state="succeeded", last_succeeded_at=now - timedelta(hours=2))
    _create_ranking_item(ranked_due, rank=1)
    _create_ranking_item(ranked_not_due, rank=2)

    service = MovieInteractionSyncService(provider=FakeProvider())

    assert _collect_candidate_movie_numbers(service, now=now) == ["RANK-001"]


def test_movie_interaction_sync_service_treats_multiple_ranking_rows_as_single_candidate(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    movie = _create_movie(
        "RANK-003",
        "rank-3",
        release_date=now - timedelta(days=365),
    )
    _create_task_state(movie, state="succeeded", last_succeeded_at=now - timedelta(hours=2))
    _create_ranking_item(movie, board_key="censored", period="daily", rank=1)
    _create_ranking_item(movie, board_key="uncensored", period="weekly", rank=1)

    service = MovieInteractionSyncService(provider=FakeProvider())
    candidates = _collect_candidate_movie_numbers(service, now=now)

    assert candidates == ["RANK-003"]


def test_movie_interaction_sync_service_falls_back_to_regular_window_after_ranking_removed(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    movie = _create_movie(
        "RANK-004",
        "rank-4",
        release_date=now - timedelta(days=365),
    )
    _create_task_state(movie, state="succeeded", last_succeeded_at=now - timedelta(hours=2))
    ranking_item = _create_ranking_item(movie, rank=1)
    service = MovieInteractionSyncService(provider=FakeProvider())

    assert _collect_candidate_movie_numbers(service, now=now) == ["RANK-004"]

    ranking_item.delete_instance()

    assert _collect_candidate_movie_numbers(service, now=now) == []


def test_movie_interaction_sync_service_updates_interaction_fields_and_heat(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    movie = _create_movie(
        "UPD-001",
        "upd-1",
        release_date=now - timedelta(days=10),
        summary="keep-summary",
        desc="keep-desc",
        score=1.5,
        score_number=1,
        watched_count=2,
        want_watch_count=3,
        comment_count=4,
        heat=0,
    )
    provider = FakeProvider(
        details={
            "upd-1": _build_detail(
                "upd-1",
                "UPD-001",
                score=4.2,
                score_number=11,
                watched_count=12,
                want_watch_count=13,
                comment_count=14,
            )
        }
    )
    service = MovieInteractionSyncService(provider=provider)
    service._now = lambda: now

    stats = service.run()
    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)

    assert stats == {
        "candidate_movies": 1,
        "processed_movies": 1,
        "succeeded_movies": 1,
        "failed_movies": 0,
        "updated_movies": 1,
        "unchanged_movies": 0,
        "heat_updated_movies": 1,
    }
    assert refreshed.score == 4.2
    assert refreshed.score_number == 11
    assert refreshed.watched_count == 12
    assert refreshed.want_watch_count == 13
    assert refreshed.comment_count == 14
    assert refreshed.heat == 12
    assert refreshed.summary == "keep-summary"
    assert refreshed.desc == "keep-desc"
    assert task_state.state == MovieInteractionSyncService.SYNC_STATUS_SUCCEEDED
    assert task_state.attempt_count == 1
    assert task_state.last_attempted_at is not None
    assert task_state.last_succeeded_at is not None
    assert task_state.last_error is None


def test_movie_interaction_sync_service_counts_unchanged_movies_without_heat_write(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    movie = _create_movie(
        "SAME-001",
        "same-1",
        release_date=now - timedelta(days=10),
        score=4.2,
        score_number=11,
        watched_count=12,
        want_watch_count=13,
        comment_count=14,
        heat=11,
    )
    provider = FakeProvider(
        details={
            "same-1": _build_detail(
                "same-1",
                "SAME-001",
                score=4.2,
                score_number=11,
                watched_count=12,
                want_watch_count=13,
                comment_count=14,
            )
        }
    )
    service = MovieInteractionSyncService(provider=provider)
    service._now = lambda: now

    stats = service.run()
    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)

    assert stats == {
        "candidate_movies": 1,
        "processed_movies": 1,
        "succeeded_movies": 1,
        "failed_movies": 0,
        "updated_movies": 0,
        "unchanged_movies": 1,
        "heat_updated_movies": 0,
    }
    assert refreshed.heat == 11
    assert task_state.state == MovieInteractionSyncService.SYNC_STATUS_SUCCEEDED
    assert task_state.attempt_count == 1
    assert task_state.last_attempted_at is not None
    assert task_state.last_succeeded_at is not None


def test_movie_interaction_sync_service_marks_failures_and_continues(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    success_movie = _create_movie(
        "OK-001",
        "ok-1",
        release_date=now - timedelta(days=10),
    )
    failed_movie = _create_movie(
        "BAD-001",
        "bad-1",
        release_date=now - timedelta(days=10),
    )
    provider = FakeProvider(
        details={
            "ok-1": _build_detail(
                "ok-1",
                "OK-001",
                score_number=10,
                want_watch_count=10,
            )
        },
        failures={"bad-1": RuntimeError("boom")},
    )
    service = MovieInteractionSyncService(provider=provider)
    service._now = lambda: now

    stats = service.run()
    refreshed_success = Movie.get_by_id(success_movie.id)
    refreshed_failed = Movie.get_by_id(failed_movie.id)
    success_state = _get_task_state(success_movie.id)
    failed_state = _get_task_state(failed_movie.id)

    assert stats == {
        "candidate_movies": 2,
        "processed_movies": 2,
        "succeeded_movies": 1,
        "failed_movies": 1,
        "updated_movies": 1,
        "unchanged_movies": 0,
        "heat_updated_movies": 1,
    }
    assert success_state.state == MovieInteractionSyncService.SYNC_STATUS_SUCCEEDED
    assert failed_state.state == MovieInteractionSyncService.SYNC_STATUS_FAILED
    assert failed_state.attempt_count == 1
    assert failed_state.last_attempted_at is not None
    assert failed_state.last_succeeded_at is None
    assert failed_state.last_error == "boom"


def test_sync_movie_ignores_due_window_and_marks_manual_state(app):
    now = datetime(2026, 4, 15, 5, 0, 0)
    movie = _create_movie(
        "MAN-001",
        "manual-1",
        release_date=now - timedelta(days=10),
        score=1.1,
        score_number=1,
        watched_count=1,
        want_watch_count=1,
        comment_count=1,
        heat=0,
    )
    _create_task_state(movie, state="succeeded", last_succeeded_at=now - timedelta(hours=1))
    provider = FakeProvider(
        details={
            "manual-1": _build_detail(
                "manual-1",
                "MAN-001",
                score=4.8,
                score_number=20,
                watched_count=21,
                want_watch_count=22,
                comment_count=23,
            )
        }
    )
    service = MovieInteractionSyncService(provider=provider)

    result = ActivityService.run_task(
        task_key="movie_interaction_sync",
        trigger_type="manual",
        func=lambda _reporter: service.sync_movie(movie),
    )

    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)

    assert result == {
        "movie_id": movie.id,
        "movie_number": "MAN-001",
        "updated_movies": 1,
        "unchanged_movies": 0,
        "heat_updated_movies": 1,
    }
    assert provider.calls == ["manual-1"]
    assert refreshed.score_number == 20
    assert refreshed.want_watch_count == 22
    assert refreshed.comment_count == 23
    assert refreshed.heat == 20
    assert task_state.state == MovieInteractionSyncService.SYNC_STATUS_SUCCEEDED
    assert task_state.last_trigger_type == "manual"


def test_sync_movie_marks_failure_and_manual_state(app):
    movie = _create_movie("MAN-002", "manual-2")
    service = MovieInteractionSyncService(provider=FakeProvider(failures={"manual-2": RuntimeError("boom")}))

    with pytest.raises(RuntimeError, match="boom"):
        ActivityService.run_task(
            task_key="movie_interaction_sync",
            trigger_type="manual",
            func=lambda _reporter: service.sync_movie(movie),
        )

    task_state = _get_task_state(movie.id)

    assert task_state.state == MovieInteractionSyncService.SYNC_STATUS_FAILED
    assert task_state.last_trigger_type == "manual"
    assert task_state.last_error == "boom"


def test_recover_interrupted_running_movies_marks_running_as_failed(app):
    running_movie = _create_movie(
        "REC-901",
        "recover-1",
    )
    _create_task_state(
        running_movie,
        state=MovieInteractionSyncService.SYNC_STATUS_RUNNING,
        attempt_count=2,
    )
    pending_movie = _create_movie(
        "REC-902",
        "recover-2",
    )
    _create_task_state(
        pending_movie,
        state=MovieInteractionSyncService.SYNC_STATUS_PENDING,
        last_error="pending_error",
    )

    recovered_count = MovieInteractionSyncService.recover_interrupted_running_movies(
        error_message="影片互动数同步任务中断，等待重试",
    )

    refreshed_running = _get_task_state(running_movie.id)
    refreshed_pending = _get_task_state(pending_movie.id)

    assert recovered_count == 1
    assert refreshed_running.state == MovieInteractionSyncService.SYNC_STATUS_FAILED
    assert refreshed_running.last_error == "影片互动数同步任务中断，等待重试"
    assert refreshed_running.attempt_count == 2
    assert refreshed_pending.state == MovieInteractionSyncService.SYNC_STATUS_PENDING
    assert refreshed_pending.last_error == "pending_error"


def test_recover_interrupted_running_movies_uses_default_error_message(app):
    running_movie = _create_movie(
        "REC-903",
        "recover-3",
    )
    _create_task_state(running_movie, state=MovieInteractionSyncService.SYNC_STATUS_RUNNING)

    recovered_count = MovieInteractionSyncService.recover_interrupted_running_movies()
    refreshed_running = _get_task_state(running_movie.id)

    assert recovered_count == 1
    assert refreshed_running.state == MovieInteractionSyncService.SYNC_STATUS_FAILED
    assert refreshed_running.last_error == MovieInteractionSyncService.INTERRUPTED_SYNC_ERROR_MESSAGE
