from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    BackgroundTaskRun,
    DownloadClient,
    DownloadSelectionPolicy,
    DownloadTask,
    Media,
    MediaLibrary,
    Movie,
)
from src.schema.transfers.download_selection import (
    DownloadSelectionKeywordGroup,
    DownloadSelectionRules,
)
from src.service.catalog.movie_subscription_search_state_service import (
    MovieSubscriptionSearchStateService,
)
from src.service.system.activity.task_summary import has_failed_summary
from src.service.transfers.downloads.auto_subscribed.auto_download_service import (
    MIN_SIZE_BYTES,
    SubscribedMovieAutoDownloadService,
)
from src.service.transfers.downloads.search_service import (
    DownloadCandidateSearchResult,
)
from src.service.transfers.downloads.selection.defaults import (
    legacy_selection_rules,
)
from src.service.transfers.downloads.selection.policy_service import (
    DownloadSelectionPolicyService,
)


def _candidate(*, title: str, source_uri: str, client_ids: tuple[int, ...] = (1,)):
    return SimpleNamespace(
        source_uri=source_uri,
        resolved_client_id=client_ids[0],
        download_clients=[
            SimpleNamespace(id=client_id, name=f"client-{client_id}")
            for client_id in client_ids
        ],
        indexer_name="indexer",
        indexer_kind="pt",
        release_title=title,
        description="",
        published_at=None,
        title=title,
        size_bytes=MIN_SIZE_BYTES,
        seeders=10,
    )


def _build_service(
    monkeypatch,
    *,
    candidates,
    response=None,
    error=None,
    media_replacement_pending: bool = False,
):
    search_service = SimpleNamespace(
        search_candidates_with_stats=Mock(
            return_value=DownloadCandidateSearchResult(
                candidates=candidates,
                raw_candidate_count=len(candidates),
                system_candidate_count=len(candidates),
            )
        )
    )
    request_service = SimpleNamespace(create_request=Mock())
    if error is not None:
        request_service.create_request.side_effect = error
    else:
        request_service.create_request.return_value = response
    service = SubscribedMovieAutoDownloadService(
        download_search_service=search_service,
        download_request_service=request_service,
    )
    movie = SimpleNamespace(
        id=1,
        movie_number="ABC-001",
        media_replacement_pending=media_replacement_pending,
    )
    monkeypatch.setattr(service, "_select_candidate_ids", Mock(return_value=[movie.id]))
    monkeypatch.setattr(service, "_library_import_running", Mock(return_value=False))
    monkeypatch.setattr(service, "_media_exists", Mock(return_value=False))
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
    monkeypatch.setattr(
        DownloadSelectionPolicyService,
        "get_resource",
        Mock(
            return_value=SimpleNamespace(
                rules=legacy_selection_rules(),
                version=1,
            )
        ),
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
    assert has_failed_summary(summary) is True


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


@pytest.mark.parametrize("code", ["provider_source_blacklisted", "download_source_blacklisted"])
def test_auto_download_tries_next_candidate_after_blacklist_rejection(monkeypatch, code):
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
        ApiError(422, code, "该资源已被拉黑"),
        response,
    ]

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["failed_items"] == []
    assert request_service.create_request.call_count == 2
    assert request_service.create_request.call_args_list[-1].args[0].candidate.source_uri == "magnet:?xt=second"


def test_auto_download_rolls_over_to_next_bound_client(monkeypatch):
    response = SimpleNamespace(created=True)
    service, request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="resource", source_uri="magnet:?xt=first", client_ids=(1, 2))],
        response=response,
    )
    request_service.create_request.side_effect = [
        ApiError(503, "provider_unavailable", "A unavailable"),
        response,
    ]

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["submitted_movies"] == 1
    assert [
        call.args[0].client_id for call in request_service.create_request.call_args_list
    ] == [1, 2]


def test_auto_download_does_not_roll_over_after_client_accepts(monkeypatch):
    response = SimpleNamespace(created=True)
    service, request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="resource", source_uri="magnet:?xt=first", client_ids=(1, 2))],
        response=response,
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["submitted_movies"] == 1
    request_service.create_request.assert_called_once()
    assert request_service.create_request.call_args.args[0].client_id == 1


def test_auto_download_passes_clean_release_title_and_description(monkeypatch):
    candidate = _candidate(
        title="ABC-001 1080p 中文字幕",
        source_uri="magnet:?xt=clean-fields",
    )
    candidate.release_title = "ABC-001 1080p"
    candidate.description = "中文字幕"
    service, request_service = _build_service(
        monkeypatch,
        candidates=[candidate],
        response=SimpleNamespace(created=True),
    )

    service.run(reporter=SimpleNamespace(emit=Mock()))

    payload = request_service.create_request.call_args.args[0]
    assert payload.candidate.release_title == "ABC-001 1080p"
    assert payload.candidate.description == "中文字幕"


def test_auto_download_applies_policy_before_candidate_attempt_limit(monkeypatch):
    response = SimpleNamespace(created=True)
    candidates = []
    for index in range(1, 6):
        candidate = _candidate(
            title=f"ordinary-{index}",
            source_uri=f"magnet:?xt=ordinary-{index}",
        )
        candidate.size_bytes = (10 - index) * MIN_SIZE_BYTES
        candidates.append(candidate)
    target = _candidate(
        title="target resource",
        source_uri="magnet:?xt=target",
    )
    target.size_bytes = MIN_SIZE_BYTES
    candidates.append(target)
    service, request_service = _build_service(
        monkeypatch,
        candidates=candidates,
        response=response,
    )
    monkeypatch.setattr(
        DownloadSelectionPolicyService,
        "get_resource",
        Mock(
            return_value=SimpleNamespace(
                rules=DownloadSelectionRules(
                    preferred_groups=[
                        DownloadSelectionKeywordGroup(
                            label="target",
                            terms=["target"],
                        )
                    ]
                ),
                version=2,
            )
        ),
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["submitted_movies"] == 1
    assert (
        request_service.create_request.call_args.args[0].candidate.source_uri
        == "magnet:?xt=target"
    )


def test_auto_download_global_blacklist_skips_other_clients_for_same_candidate(monkeypatch):
    response = SimpleNamespace(created=True)
    service, request_service = _build_service(
        monkeypatch,
        candidates=[
            _candidate(title="first", source_uri="magnet:?xt=first", client_ids=(1, 2)),
            _candidate(title="second", source_uri="magnet:?xt=second", client_ids=(3,)),
        ],
        response=response,
    )
    request_service.create_request.side_effect = [
        ApiError(422, "download_source_blacklisted", "blacklisted"),
        response,
    ]

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["submitted_movies"] == 1
    assert [
        call.args[0].client_id for call in request_service.create_request.call_args_list
    ] == [1, 3]


def test_auto_download_replacement_skips_reused_task_and_tries_next_candidate(monkeypatch):
    service, request_service = _build_service(
        monkeypatch,
        candidates=[
            _candidate(title="first", source_uri="magnet:?xt=first"),
            _candidate(title="second", source_uri="magnet:?xt=second"),
        ],
        media_replacement_pending=True,
    )
    # 复用已有远端任务不会重新下载也不会重跑导入，补库必须换下一个候选。
    request_service.create_request.side_effect = [
        SimpleNamespace(created=False),
        SimpleNamespace(created=True),
    ]

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["submitted_movies"] == 1
    assert [
        call.args[0].candidate.source_uri
        for call in request_service.create_request.call_args_list
    ] == ["magnet:?xt=first", "magnet:?xt=second"]


def test_auto_download_keeps_reused_task_as_skipped_without_replacement(monkeypatch):
    service, request_service = _build_service(
        monkeypatch,
        candidates=[
            _candidate(title="first", source_uri="magnet:?xt=first"),
            _candidate(title="second", source_uri="magnet:?xt=second"),
        ],
        response=SimpleNamespace(created=False),
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["submitted_movies"] == 0
    assert summary["skipped_movies"] == 1
    request_service.create_request.assert_called_once()
    assert request_service.create_request.call_args.args[0].candidate.source_uri == "magnet:?xt=first"


def _task_for_selector(movie: Movie, *, state: str, import_status: str) -> DownloadTask:
    library = MediaLibrary.create(
        name=f"library-{movie.id}",
        provider_key="test",
        provider_config={},
    )
    client = DownloadClient.create(
        name=f"client-{movie.id}",
        library=library,
        provider_config={},
    )
    return DownloadTask.create(
        client=client,
        movie=movie.movie_number,
        remote_id=f"remote-{movie.id}",
        name=movie.movie_number,
        state=state,
        progress=1,
        import_status=import_status,
    )


def test_auto_selector_allows_replacement_with_completed_history(test_db):
    movie = Movie.create(
        movie_number="REPLACE-001",
        javdb_id="replace-001",
        title="replace",
        is_subscribed=True,
        media_replacement_pending=True,
    )
    _task_for_selector(movie, state="completed", import_status="completed")

    assert SubscribedMovieAutoDownloadService._select_candidate_ids() == [movie.id]


def test_auto_selector_keeps_zero_output_import_blocked_without_replacement(test_db):
    movie = Movie.create(
        movie_number="REPLACE-002",
        javdb_id="replace-002",
        title="zero output",
        is_subscribed=True,
    )
    _task_for_selector(movie, state="completed", import_status="completed")

    assert SubscribedMovieAutoDownloadService._select_candidate_ids() == []


def test_auto_selector_keeps_active_task_blocked_during_replacement(test_db):
    movie = Movie.create(
        movie_number="REPLACE-003",
        javdb_id="replace-003",
        title="active",
        is_subscribed=True,
        media_replacement_pending=True,
    )
    _task_for_selector(movie, state="downloading", import_status="pending")

    assert SubscribedMovieAutoDownloadService._select_candidate_ids() == []


def test_auto_selector_keeps_unfinished_import_blocked_during_replacement(test_db):
    movie = Movie.create(
        movie_number="REPLACE-004",
        javdb_id="replace-004",
        title="unfinished import",
        is_subscribed=True,
        media_replacement_pending=True,
    )
    _task_for_selector(movie, state="completed", import_status="running")

    assert SubscribedMovieAutoDownloadService._select_candidate_ids() == []


def test_auto_selector_allows_retry_after_failed_download(test_db):
    movie = Movie.create(
        movie_number="RETRY-001",
        javdb_id="retry-001",
        title="failed download",
        is_subscribed=True,
    )
    # 下载失败的任务仍带着初始的 import_status=pending，不能当成"导入在途"挡住下一轮搜索。
    _task_for_selector(movie, state="failed", import_status="pending")

    assert SubscribedMovieAutoDownloadService._select_candidate_ids() == [movie.id]


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
        download_search_service=SimpleNamespace(
            search_candidates_with_stats=Mock(
                return_value=DownloadCandidateSearchResult(
                    candidates=[],
                    raw_candidate_count=0,
                    system_candidate_count=0,
                )
            )
        ),
        download_request_service=SimpleNamespace(create_request=Mock()),
    )
    reporter = SimpleNamespace(emit=Mock())

    for _ in range(3):
        service.run(reporter=reporter)

    stored = Movie.get_by_id(movie.id)
    assert stored.subscription_search_state == "exhausted"
    assert stored.subscription_search_attempt_count == 3
    assert stored.subscription_search_error_code == "no_candidate_found"


def test_auto_download_policy_filter_does_not_consume_retry_budget(test_db):
    movie = Movie.create(
        movie_number="POLICY-001",
        javdb_id="policy-001",
        title="policy",
        is_subscribed=True,
    )
    DownloadSelectionPolicy.create(
        id=1,
        version=1,
        rules=DownloadSelectionRules(
            required_groups=[
                DownloadSelectionKeywordGroup(label="required", terms=["TARGET"])
            ]
        ).model_dump(mode="json"),
    )
    candidate = _candidate(
        title="POLICY-001 ordinary",
        source_uri="magnet:?xt=ordinary",
    )
    request_service = SimpleNamespace(create_request=Mock())
    service = SubscribedMovieAutoDownloadService(
        download_search_service=SimpleNamespace(
            search_candidates_with_stats=Mock(
                return_value=DownloadCandidateSearchResult(
                    candidates=[candidate],
                    raw_candidate_count=1,
                    system_candidate_count=1,
                )
            )
        ),
        download_request_service=request_service,
    )

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    stored = Movie.get_by_id(movie.id)
    assert stored.subscription_search_state == "failed_retryable"
    assert stored.subscription_search_attempt_count == 0
    assert stored.subscription_search_error_code == "filtered_by_policy"
    assert summary["policy_filtered_movies"] == 1
    assert summary["policy_filtered_movie_numbers"] == ["POLICY-001"]
    assert summary["no_candidate_movies"] == 0
    assert summary["raw_candidate_count"] == 1
    assert summary["system_candidate_count"] == 1
    assert summary["policy_eligible_candidate_count"] == 0
    assert summary["policy_rejection_counts"] == {"required_group_missing": 1}
    request_service.create_request.assert_not_called()


def test_auto_download_skips_when_library_import_running(monkeypatch):
    service, request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="candidate", source_uri="provider://candidate")],
        response=None,
    )
    monkeypatch.setattr(service, "_library_import_running", Mock(return_value=True))

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["skipped"] is True
    assert summary["reason"] == "library_import_running"
    assert summary["candidate_movies"] == 0
    assert summary["failed_movies"] == 0
    service._select_candidate_ids.assert_not_called()
    request_service.create_request.assert_not_called()


def test_library_import_guard_only_counts_running_state(test_db):
    assert SubscribedMovieAutoDownloadService._library_import_running() is False

    BackgroundTaskRun.create(
        task_key="library_import",
        task_name="pending import",
        trigger_type="manual",
        state="pending",
    )
    assert SubscribedMovieAutoDownloadService._library_import_running() is False

    BackgroundTaskRun.create(
        task_key="library_import",
        task_name="running import",
        trigger_type="manual",
        state="running",
    )
    assert SubscribedMovieAutoDownloadService._library_import_running() is True


def test_auto_download_skips_submission_when_media_exists(monkeypatch):
    service, request_service = _build_service(
        monkeypatch,
        candidates=[_candidate(title="candidate", source_uri="provider://candidate")],
        response=None,
    )
    monkeypatch.setattr(service, "_media_exists", Mock(return_value=True))

    summary = service.run(reporter=SimpleNamespace(emit=Mock()))

    assert summary["searched_movies"] == 1
    assert summary["skipped_movies"] == 1
    assert summary["submitted_movies"] == 0
    assert summary["no_candidate_movies"] == 0
    request_service.create_request.assert_not_called()


def test_media_exists_detects_media_row(test_db):
    library = MediaLibrary.create(
        name="auto-download-media-library", provider_key="test", provider_config={}
    )
    movie = Movie.create(movie_number="MEDIA-001", title="media")
    assert SubscribedMovieAutoDownloadService._media_exists("MEDIA-001") is False

    Media.create(
        library=library,
        movie=movie,
        file_name="MEDIA-001.mp4",
        file_size_bytes=100,
    )
    assert SubscribedMovieAutoDownloadService._media_exists("MEDIA-001") is True
