from datetime import datetime

from src.model import (
    DownloadResourceBlacklist,
    DownloadSelectionPolicy,
    DownloadSubmissionRecord,
)
from src.schema.transfers.downloads import DownloadCandidateResource
from src.service.transfers.downloads.search_service import (
    DownloadCandidateSearchResult,
    DownloadSearchService,
)


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _candidate(title: str = "ABC-001 1080p", *, size_gib: int = 5):
    return DownloadCandidateResource(
        source_uri=f"magnet:?xt={title}",
        indexer_name="indexer-a",
        indexer_kind="pt",
        resolved_client_id=1,
        resolved_client_name="client-a",
        download_clients=[],
        movie_number="ABC-001",
        title=title,
        release_title=title,
        description="",
        published_at=datetime(2026, 10, 29, 12, 0, 0),
        size_bytes=size_gib * 1024**3,
        seeders=10,
    )


def test_download_selection_policy_get_update_and_version_conflict(
    client,
    account_user,
):
    token = _login(client, account_user.username)
    headers = _auth(token)

    initial = client.get("/download-selection-policy", headers=headers)
    assert initial.status_code == 200, initial.text
    assert initial.json()["version"] == 1
    assert initial.json()["rules"]["ranking"] == ["size_desc"]

    updated = client.put(
        "/download-selection-policy",
        headers=headers,
        json={
            "expected_version": 1,
            "rules": {
                "required_groups": [
                    {"label": "quality", "terms": ["1080p", "2160p"]}
                ],
                "excluded_terms": ["sample"],
                "ranking": ["seeders_desc", "size_desc"],
            },
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["rules"]["excluded_terms"] == ["sample"]
    assert DownloadSelectionPolicy.get_by_id(1).version == 2

    conflict = client.put(
        "/download-selection-policy",
        headers=headers,
        json={
            "expected_version": 1,
            "rules": {"ranking": ["size_desc"]},
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == (
        "download_selection_policy_version_conflict"
    )


def test_download_selection_preview_uses_draft_without_persisting(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, account_user.username)
    candidates = [
        _candidate("ABC-001 1080p"),
        _candidate("ABC-001 1080p sample"),
    ]
    monkeypatch.setattr(
        DownloadSearchService,
        "search_candidates_with_stats",
        lambda self, **_kwargs: DownloadCandidateSearchResult(
            candidates=candidates,
            raw_candidate_count=2,
            system_candidate_count=2,
        ),
    )

    response = client.post(
        "/download-selection-policy/preview",
        headers=_auth(token),
        json={
            "movie_number": "ABC-001",
            "rules": {
                "required_groups": [
                    {"label": "quality", "terms": ["1080p"]}
                ],
                "excluded_terms": ["sample"],
            },
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"] is True
    assert body["policy_version"] is None
    assert body["raw_candidate_count"] == 2
    assert body["system_candidate_count"] == 2
    assert body["eligible_candidate_count"] == 1
    assert body["policy_filtered_count"] == 1
    assert body["rejection_counts"] == {"excluded_term": 1}
    assert body["selected_source_uri"] == candidates[0].source_uri
    assert body["candidates"][0]["selection_evaluation"]["within_attempt_limit"] is True
    assert DownloadSelectionPolicy.select().count() == 0


def test_download_selection_policy_rejects_unknown_indexer_reference(
    client,
    account_user,
):
    token = _login(client, account_user.username)
    headers = _auth(token)
    client.get("/download-selection-policy", headers=headers)

    response = client.put(
        "/download-selection-policy",
        headers=headers,
        json={
            "expected_version": 1,
            "rules": {"indexer_names": ["missing-indexer"]},
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == (
        "invalid_download_selection_indexers"
    )


def test_manual_candidate_search_is_annotated_but_not_blocked(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, account_user.username)
    client.get("/download-selection-policy", headers=_auth(token))
    client.put(
        "/download-selection-policy",
        headers=_auth(token),
        json={
            "expected_version": 1,
            "rules": {"excluded_terms": ["sample"]},
        },
    )
    candidate = _candidate("ABC-001 sample")
    monkeypatch.setattr(
        DownloadSearchService,
        "search_candidates",
        lambda self, **_kwargs: [candidate],
    )

    response = client.get(
        "/download-candidates",
        params={"movie_number": "ABC-001"},
        headers=_auth(token),
    )

    assert response.status_code == 200, response.text
    evaluation = response.json()[0]["selection_evaluation"]
    assert evaluation["eligible"] is False
    assert evaluation["rejection_reasons"][0]["code"] == "excluded_term"


def test_download_resource_blacklist_is_read_only_and_joined_to_latest_submission(
    client,
    account_user,
):
    token = _login(client, account_user.username)
    record = DownloadSubmissionRecord.create(
        client_id=9,
        movie_number="ABC-001",
        indexer_name="indexer-a",
        title="ABC-001 1080p",
        source_uri="magnet:?xt=abc",
        info_hash="a" * 40,
    )
    DownloadResourceBlacklist.create(info_hash=record.info_hash)

    response = client.get(
        "/download-resource-blacklist",
        headers=_auth(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["info_hash"] == record.info_hash
    assert body["items"][0]["latest_submission"]["movie_number"] == "ABC-001"
    assert body["items"][0]["latest_submission"]["task_id"] is None

    delete_response = client.delete(
        "/download-resource-blacklist",
        headers=_auth(token),
    )
    assert delete_response.status_code == 405
