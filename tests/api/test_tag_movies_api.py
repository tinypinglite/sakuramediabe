from src.model import Movie, MovieTag, Tag


def test_tag_movies_uses_base_movie_list_response(client, account_user):
    movie = Movie.create(
        movie_number="TAG-001",
        title="title",
    )
    tag = Tag.create(name="tag")
    MovieTag.create(movie=movie, tag=tag)
    login = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = client.get(f"/tags/{tag.id}/movies", headers=headers)

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["movie_number"] == "TAG-001"
    assert "my_score" not in item
    assert "my_rating_values" not in item
