from datetime import datetime

from src.api.exception.errors import ApiError
from src.schema.discovery import ImageSearchSessionPageResource, ImageSearchSessionResource


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


class _FakeImageSearchService:
    def __init__(self):
        self.calls = []

    def create_session_and_first_page(self, **kwargs):
        self.calls.append(("create", kwargs))
        return ImageSearchSessionPageResource(
            session_id="session-1",
            status="ready",
            page_size=20,
            next_cursor="cursor-1",
            expires_at=datetime(2026, 3, 13, 10, 0, 0),
            items=[],
        )

    def get_session(self, session_id):
        self.calls.append(("get", session_id))
        return ImageSearchSessionResource(
            session_id=session_id,
            status="ready",
            page_size=20,
            next_cursor="cursor-1",
            expires_at=datetime(2026, 3, 13, 10, 0, 0),
        )

    def list_results(self, session_id, cursor=None):
        self.calls.append(("results", session_id, cursor))
        return ImageSearchSessionPageResource(
            session_id=session_id,
            status="ready",
            page_size=20,
            next_cursor=None,
            expires_at=datetime(2026, 3, 13, 10, 0, 0),
            items=[],
        )


def test_image_search_router_requires_authentication(client):
    create_response = client.post("/image-search/sessions")
    session_response = client.get("/image-search/sessions/session-1")
    results_response = client.get("/image-search/sessions/session-1/results")

    assert create_response.status_code == 401
    assert session_response.status_code == 401
    assert results_response.status_code == 401


def test_create_image_search_session_rejects_empty_file(client, account_user):
    token = _login(client, username=account_user.username)

    response = client.post(
        "/image-search/sessions",
        files={"file": ("query.png", b"", "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "http_error"


def test_image_search_routes_call_service(client, account_user, monkeypatch):
    token = _login(client, username=account_user.username)
    fake_service = _FakeImageSearchService()
    monkeypatch.setattr(
        "src.api.routers.discovery.image_search.get_image_search_service",
        lambda: fake_service,
    )

    create_response = client.post(
        "/image-search/sessions",
        files={"file": ("query.png", b"abc", "image/png")},
        data={
            "page_size": 20,
            "movie_ids": "1,2",
            "exclude_movie_ids": "3",
            "score_threshold": "0.7",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    session_response = client.get(
        "/image-search/sessions/session-1",
        headers={"Authorization": f"Bearer {token}"},
    )
    results_response = client.get(
        "/image-search/sessions/session-1/results?cursor=cursor-1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert create_response.status_code == 200
    assert session_response.status_code == 200
    assert results_response.status_code == 200
    assert fake_service.calls[0][0] == "create"
    assert fake_service.calls[1] == ("get", "session-1")
    assert fake_service.calls[2] == ("results", "session-1", "cursor-1")


def test_create_image_search_session_returns_503_when_inference_service_is_unavailable(
    client,
    account_user,
    monkeypatch,
):
    token = _login(client, username=account_user.username)

    class _FailingService:
        def create_session_and_first_page(self, **_kwargs):
            raise ApiError(503, "image_search_inference_unavailable", "inference service unavailable")

    monkeypatch.setattr(
        "src.api.routers.discovery.image_search.get_image_search_service",
        lambda: _FailingService(),
    )

    response = client.post(
        "/image-search/sessions",
        files={"file": ("query.png", b"abc", "image/png")},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "image_search_inference_unavailable"
