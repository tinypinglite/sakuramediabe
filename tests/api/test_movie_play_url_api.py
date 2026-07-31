"""影片播放链接解析端点（GET /media/play-url）API 测试。

验证：按播放源（本地/115）与播放模式（单个/合并）返回正确的签名链接；
本地多分段返回合并 URL 且签名锚定首个分段；115 合并返回 HLS 代理 m3u8；
无媒体/参数缺失的防御行为。
"""

import hmac
import hashlib
import uuid
from urllib.parse import parse_qs, urlparse

from src.model import Media, MediaLibrary, Movie
from src.model.enums import MediaLibraryBackend

from tests.conftest import TEST_FILE_SIGNATURE_EXPIRES, TEST_FILE_SIGNATURE_SECRET


PLAY_URL_PATH = "/media/play-url"


def _unique() -> str:
    return uuid.uuid4().hex[:10]


def _login(client, username: str) -> str:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return response.json()["access_token"]


def _auth_headers(client, username: str) -> dict:
    return {"Authorization": f"Bearer {_login(client, username)}"}


def _create_movie(movie_number: str) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
    )


def _create_media(movie: Movie, *, library=None, valid: bool = True, **kwargs) -> Media:
    if library is None:
        library, _ = MediaLibrary.get_or_create(
            name="test-local-lib",
            defaults={"backend": "local", "backend_config": {"root_path": "/library"}},
        )
    tag = _unique()
    return Media.create(
        movie=movie,
        library=library,
        path=f"/library/{movie.movie_number}-{tag}.mp4",
        valid=valid,
        **kwargs,
    )


def _create_cloud_library() -> MediaLibrary:
    return MediaLibrary.create(
        name="test-115-lib", backend=MediaLibraryBackend.CLOUD115.value
    )


def _merged_signature(first_media_id: int) -> str:
    return hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        f"media:{first_media_id}:{TEST_FILE_SIGNATURE_EXPIRES}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _query(response, key: str):
    return parse_qs(urlparse(response.json()["play_url"]).query)[key][0]


class TestMoviePlayUrlApi:
    def test_local_merged_returns_merged_url(self, client, account_user):
        movie = _create_movie("ABC-001")
        m1 = _create_media(movie)
        m2 = _create_media(movie)

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-001", "source": "local", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "merged_local"
        assert payload["segment_count"] == 2
        assert [s["media_id"] for s in payload["segments"]] == [m1.id, m2.id]

        url = payload["play_url"]
        assert url.startswith("/media/merged-stream?media_ids=")
        assert _query(response, "media_ids") == f"{m1.id},{m2.id}"
        assert _query(response, "expires") == str(TEST_FILE_SIGNATURE_EXPIRES)
        assert _query(response, "signature") == _merged_signature(m1.id)

    def test_local_merged_single_segment_falls_back(self, client, account_user):
        movie = _create_movie("ABC-002")
        m1 = _create_media(movie)

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-002", "source": "local", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "single_local"
        assert payload["play_url"].startswith(f"/media/{m1.id}/stream?")
        assert _query(response, "expires") == str(TEST_FILE_SIGNATURE_EXPIRES)
        assert _query(response, "signature") == _merged_signature(m1.id)

    def test_local_single_returns_first_segment(self, client, account_user):
        movie = _create_movie("ABC-003")
        m1 = _create_media(movie)
        m2 = _create_media(movie)

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-003", "source": "local", "mode": "single"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "single_local"
        assert payload["play_url"].startswith(f"/media/{m1.id}/stream?")
        assert _query(response, "expires") == str(TEST_FILE_SIGNATURE_EXPIRES)
        assert _query(response, "signature") == _merged_signature(m1.id)
        assert payload["segment_count"] == 1

    def test_cloud115_merged_returns_merged_hls_url(self, client, account_user):
        movie = _create_movie("ABC-004")
        cloud_lib = _create_cloud_library()
        m1 = _create_media(movie, library=cloud_lib, backend_locator={"fid": _unique()})
        m2 = _create_media(movie, library=cloud_lib, backend_locator={"fid": _unique()})

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-004", "source": "cloud115", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "cloud115_merged"
        assert payload["segment_count"] == 2
        assert [s["media_id"] for s in payload["segments"]] == [m1.id, m2.id]

        url = payload["play_url"]
        assert url.startswith("/media/merged-stream.m3u8?media_ids=")
        assert _query(response, "media_ids") == f"{m1.id},{m2.id}"
        assert _query(response, "expires") == str(TEST_FILE_SIGNATURE_EXPIRES)
        assert _query(response, "signature") == _merged_signature(m1.id)

    def test_cloud115_single_returns_first_segment(self, client, account_user):
        movie = _create_movie("ABC-005")
        cloud_lib = _create_cloud_library()
        m1 = _create_media(movie, library=cloud_lib, backend_locator={"fid": _unique()})
        _create_media(movie, library=cloud_lib, backend_locator={"fid": _unique()})

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-005", "source": "cloud115", "mode": "single"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "single_cloud115"
        assert payload["play_url"].startswith(f"/media/{m1.id}/stream?")
        assert _query(response, "expires") == str(TEST_FILE_SIGNATURE_EXPIRES)
        assert _query(response, "signature") == _merged_signature(m1.id)

    def test_invalid_media_excluded_from_candidates(self, client, account_user):
        movie = _create_movie("ABC-011")
        good1 = _create_media(movie)
        _create_media(movie, valid=False)
        good2 = _create_media(movie)

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-011", "source": "local", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["kind"] == "merged_local"
        assert [s["media_id"] for s in payload["segments"]] == [good1.id, good2.id]

    def test_orphan_cloud_media_not_treated_as_local(self, client, account_user):
        # library 被 SET NULL 的云端孤儿行：path 为空但 backend_locator 仍在，
        # 本地源不应把它当成本地媒体给出必然 404 的坏链接。
        movie = _create_movie("ABC-012")
        Media.create(movie=movie, backend_locator={"fid": _unique()})

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-012", "source": "local", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "none"
        assert response.json()["play_url"] is None

    def test_source_without_media_returns_none(self, client, account_user):
        # 只有 115 媒体、本地源无媒体的影片，本地源应返回 none 而不是坏链接。
        cloud_lib = _create_cloud_library()
        cloud_only = _create_movie("ABC-007")
        _create_media(cloud_only, library=cloud_lib, backend_locator={"fid": _unique()})

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-007", "source": "local", "mode": "single"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "none"
        assert response.json()["play_url"] is None

    def test_no_media_returns_none(self, client, account_user):
        _create_movie("ABC-008")

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-008", "source": "local", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "none"
        assert response.json()["play_url"] is None

    def test_movie_id_lookup(self, client, account_user):
        movie = _create_movie("ABC-009")
        _create_media(movie)
        _create_media(movie)

        response = client.get(
            PLAY_URL_PATH,
            params={"movie_id": movie.id, "source": "local", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 200
        assert response.json()["kind"] == "merged_local"

    def test_missing_movie_filter_rejected(self, client, account_user):
        response = client.get(
            PLAY_URL_PATH,
            params={"source": "local", "mode": "merged"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_movie_filter"

    def test_movie_not_found(self, client, account_user):
        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-999", "source": "local", "mode": "single"},
            headers=_auth_headers(client, account_user.username),
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "movie_not_found"

    def test_requires_auth(self, client, account_user):
        _create_movie("ABC-010")
        response = client.get(
            PLAY_URL_PATH,
            params={"movie_number": "ABC-010", "source": "local", "mode": "single"},
        )
        assert response.status_code == 401
