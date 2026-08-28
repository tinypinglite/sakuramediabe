from types import SimpleNamespace
from unittest.mock import Mock

from src.api.exception.errors import ApiError
from src.model import Movie
from src.service.catalog.movie_subscription_search_state_service import (
    MovieSubscriptionSearchStateService,
)
from src.service.system.activity.notifications import _detect_failed_summary
from src.service.transfers.downloads.auto_subscribed.auto_download_service import (
    MIN_SIZE_BYTES,
    SubscribedMovieAutoDownloadService,
)


def _candidate(*, title: str, source_uri: str):
    return SimpleNamespace(
        source_uri=source_uri,
        resolved_client_id=1,
        indexer_name="indexer",
        title=title,
        size_bytes=MIN_SIZE_BYTES,
        seeders=10,
    )


def _build_service(monkeypatch, *, candidates, response=None, error=None):
    search_service = SimpleNamespace(search_candidates=Mock(return_value=candidates))
    request_service = SimpleNamespace(create_request=Mock())
    if error is not None:
        request_service.create_request.side_effect = error
    else:
        request_service.create_request.return_value = response
    service = SubscribedMovieAutoDownloadService(
        download_search_service=search_service,
        download_request_service=request_service,
    )
    movie = SimpleNamespace(id=1, movie_number="ABC-001")
    monkeypatch.setattr(service, "_select_candidate_ids", Mock(return_value=[movie.id]))
    monkeypatch.setattr(
        MovieSubscriptionSearchStateService,
        "begin_attempt",
        Mock(return_value=movie),
    )
    monkeypatch.setattr(
        MovieSubscriptionSearchStateService,
        "mark_succeeded",
        Mock(),
    )
    monkeypatch.setattr(
        MovieSubscriptionSearchStateService,
        "mark_failed",
        Mock(),
    )
    return service, request_service


def test_auto_download_records_candidate_conversion_failure_as_process(monkeypatch):
    from src.service.transfers.downloads.auto_subscribed import auto_download_service

    service, _request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="bad", source_uri="provider://bad")],
        response=None,
    )
    monkeypatch.setattr(
        auto_download_service,
        "DownloadRequestCreateRequest",
        Mock(side_effect=ValueError("bad candidate")),
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["failed_movies"] == 1
    assert summary["failed_items"] == [
        {"movie_number": "ABC-001", "stage": "process", "detail": "bad candidate"}
    ]
    assert _detect_failed_summary(summary) is True


def test_auto_download_records_response_handling_failure_as_process(monkeypatch):
    class BrokenResponse:
        @property
        def created(self):
            raise ValueError("bad response")

    service, _request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="bad response", source_uri="provider://bad-response")],
        response=BrokenResponse(),
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["failed_items"] == [
        {"movie_number": "ABC-001", "stage": "process", "detail": "bad response"}
    ]


def test_auto_download_does_not_duplicate_submit_failure(monkeypatch):
    service, _request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="submit", source_uri="provider://submit")],
        error=ApiError(500, "provider_submit_failed", "submit exploded"),
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["failed_movies"] == 1
    assert len(summary["failed_items"]) == 1
    assert summary["failed_items"][0]["stage"] == "submit"


def test_auto_download_tries_next_candidate_after_blacklist_rejection(monkeypatch):
    response = SimpleNamespace(created=False)
    service, request_service = _build_service(
        monkeypatch,
        candidates=[
            _candidate(title="first", source_uri="magnet:?xt=first"),
            _candidate(title="second", source_uri="magnet:?xt=second"),
        ],
        response=response,
    )
    request_service.create_request.side_effect = [
        ApiError(422, "provider_source_blacklisted", "该种子已被标记为死种"),
        response,
    ]

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["failed_items"] == []
    assert request_service.create_request.call_count == 2
    assert request_service.create_request.call_args_list[-1].args[0].candidate.source_uri == "magnet:?xt=second"


def test_auto_download_exhausts_old_movie_after_configured_no_candidate_budget(
    test_db, monkeypatch
):
    movie = Movie.create(
        movie_number="STATE-001",
        javdb_id="state-001",
        title="state",
        is_subscribed=True,
    )
    service = SubscribedMovieAutoDownloadService(
        download_search_service=SimpleNamespace(search_candidates=Mock(return_value=[])),
        download_request_service=SimpleNamespace(create_request=Mock()),
    )
    reporter = SimpleNamespace(emit=Mock())

    for _ in range(3):
        service.run(reporter=reporter)

    stored = Movie.get_by_id(movie.id)
    assert stored.subscription_search_state == "exhausted"
    assert stored.subscription_search_attempt_count == 3
    assert stored.subscription_search_error_code == "no_candidate_found"
