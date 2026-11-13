from src.api.exception.errors import ApiError
from src.model import Movie
from src.service.catalog import MovieMetadataRefreshService, MovieService


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_refresh_movie_metadata_returns_latest_detail(
    client, account_user, monkeypatch
):
    movie = Movie.create(movie_number="ABC-001", title="旧标题")
    headers = _auth_headers(client, account_user)
    monkeypatch.setattr(
        MovieMetadataRefreshService,
        "refresh_movie_metadata",
        classmethod(
            lambda _cls, movie_number: MovieService.get_movie_detail(movie_number)
        ),
    )

    response = client.post(
        "/movies/ABC-001/metadata-refresh", headers=headers
    )

    assert response.status_code == 200
    assert response.json()["movie_number"] == movie.movie_number
    assert response.json()["title"] == "旧标题"


def test_refresh_movie_metadata_preserves_api_error_code(
    client, account_user, monkeypatch
):
    Movie.create(movie_number="ABC-001", title="旧标题")
    headers = _auth_headers(client, account_user)

    def missing(_cls, _movie_number):
        raise ApiError(
            404,
            "movie_metadata_not_found",
            "影片远端元数据不存在",
        )

    monkeypatch.setattr(
        MovieMetadataRefreshService,
        "refresh_movie_metadata",
        classmethod(missing),
    )

    response = client.post(
        "/movies/ABC-001/metadata-refresh", headers=headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "movie_metadata_not_found"
