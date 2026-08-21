from types import SimpleNamespace
from unittest.mock import Mock

from src.model import Movie
from src.service.catalog.movie_subscription_search_state_service import (
    MovieSubscriptionSearchStateService,
)
from src.service.system.activity.notifications import _detect_failed_summary
from src.service.transfers.downloads.auto_subscribed.auto_download_service import (
    MIN_SIZE_BYTES,
    SubscribedMovieAutoDownloadService,
)


def _candidate(*, title: str, info_hash: str):
    return SimpleNamespace(
        source="torznab",
        indexer_name="indexer",
        indexer_kind="torznab",
        title=title,
        size_bytes=MIN_SIZE_BYTES,
        seeders=10,
        magnet_url=f"magnet:?xt=urn:btih:{info_hash}",
        torrent_url="",
        info_hash=info_hash,
        tags=[],
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
    monkeypatch.setattr(service, "_cloud115_client_ids", Mock(return_value=set()))
    monkeypatch.setattr(service, "_list_dead_info_hashes", Mock(return_value=set()))
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


def test_auto_download_excludes_dead_hash_before_submit(monkeypatch):
    dead_hash = "1" * 40
    alive_hash = "2" * 40
    response = SimpleNamespace(
        created=False,
        task=SimpleNamespace(client_id=1, name="existing", info_hash=alive_hash),
    )
    service, request_service = _build_service(
        monkeypatch,
        candidates=[
            _candidate(title="dead", info_hash=dead_hash),
            _candidate(title="alive", info_hash=alive_hash),
        ],
        response=response,
    )
    monkeypatch.setattr(service, "_list_dead_info_hashes", Mock(return_value={dead_hash}))

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert request_service.create_request.call_args.args[0].candidate.info_hash == alive_hash
    assert summary["failed_items"] == []


def test_auto_download_records_candidate_conversion_failure_as_process(monkeypatch):
    service, _request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="bad", info_hash="3" * 40)],
        response=None,
    )
    monkeypatch.setattr(
        service, "_build_candidate_payload", Mock(side_effect=ValueError("bad candidate"))
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
        candidates=[_candidate(title="bad response", info_hash="4" * 40)],
        response=BrokenResponse(),
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["failed_items"] == [
        {"movie_number": "ABC-001", "stage": "process", "detail": "bad response"}
    ]


def test_auto_download_does_not_duplicate_submit_failure(monkeypatch):
    service, _request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="submit", info_hash="5" * 40)],
        error=RuntimeError("submit exploded"),
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["failed_movies"] == 1
    assert len(summary["failed_items"]) == 1
    assert summary["failed_items"][0]["stage"] == "submit"


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
    monkeypatch.setattr(service, "_cloud115_client_ids", Mock(return_value=set()))
    monkeypatch.setattr(service, "_list_dead_info_hashes", Mock(return_value=set()))
    reporter = SimpleNamespace(emit=Mock())

    for _ in range(3):
        service.run(reporter=reporter)

    stored = Movie.get_by_id(movie.id)
    assert stored.subscription_search_state == "exhausted"
    assert stored.subscription_search_attempt_count == 3
    assert stored.subscription_search_error_code == "no_candidate_found"
