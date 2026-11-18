from src.model import Media, MediaLibrary, Movie


def test_movie_resolution_filter_and_invalid_value(client, account_user):
    token = client.post("/auth/tokens", json={
        "username": account_user.username, "password": "password123",
    }).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    library = MediaLibrary.create(name="Resolution", provider_key="not-installed")
    movie = Movie.create(movie_number="RES-001", title="Resolution")
    Media.create(movie=movie, library=library, file_name="4k.mp4", resolution="3840x2160")
    Media.create(movie=movie, library=library, file_name="1080.mp4", resolution="1920x1080")
    for resolution, total in [("4K", 1), ("1080P", 0), ("8K", 0)]:
        response = client.get("/movies", params={"resolution": resolution}, headers=headers)
        assert response.status_code == 200
        assert response.json()["total"] == total
        assert len(response.json()["items"]) == total
    response = client.get("/movies", params={"resolution": "invalid"}, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"
