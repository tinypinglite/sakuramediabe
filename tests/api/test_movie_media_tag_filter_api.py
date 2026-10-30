from src.model import (
    Media,
    MediaLibrary,
    MediaMediaTag,
    MediaTag,
    Movie,
    MovieTag,
    Tag,
)


def _auth_headers(client, account_user) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _movie(number: str) -> Movie:
    return Movie.create(movie_number=number, title=number)


def _numbers(response) -> set[str]:
    assert response.status_code == 200, response.text
    return {item["movie_number"] for item in response.json()["items"]}


def test_movie_media_tag_filter_supports_or_and_without_duplicate_totals(
    client,
    account_user,
):
    headers = _auth_headers(client, account_user)
    library = MediaLibrary.create(
        name="movie-tag-filter",
        provider_key="test",
        provider_config={},
    )
    tag_a = MediaTag.create(name="A", include_terms=["a"], exclude_terms=[])
    tag_b = MediaTag.create(name="B", include_terms=["b"], exclude_terms=[])

    first = _movie("MTAG-001")
    second = _movie("MTAG-002")
    split = _movie("MTAG-003")
    invalid = _movie("MTAG-004")

    media_first = Media.create(movie=first, library=library, file_name="MTAG-001.mp4")
    media_second = Media.create(movie=second, library=library, file_name="MTAG-002.mp4")
    split_a = Media.create(movie=split, library=library, file_name="MTAG-003-a.mp4")
    split_b = Media.create(movie=split, library=library, file_name="MTAG-003-b.mp4")
    invalid_media = Media.create(
        movie=invalid,
        library=library,
        file_name="MTAG-004.mp4",
        valid=False,
    )
    for media, tag in (
        (media_first, tag_a),
        (media_second, tag_a),
        (media_second, tag_b),
        (split_a, tag_a),
        (split_b, tag_b),
        (invalid_media, tag_a),
    ):
        MediaMediaTag.create(media=media, tag=tag)

    or_response = client.get(
        "/movies",
        headers=headers,
        params={"media_tag_ids": f"{tag_a.id},{tag_b.id}"},
    )
    assert _numbers(or_response) == {"MTAG-001", "MTAG-002", "MTAG-003"}
    assert or_response.json()["total"] == 3

    and_response = client.get(
        "/movies",
        headers=headers,
        params={
            "media_tag_ids": f"{tag_a.id},{tag_b.id}",
            "media_tag_match": "and",
        },
    )
    assert _numbers(and_response) == {"MTAG-002", "MTAG-003"}
    assert and_response.json()["total"] == 2


def test_movie_media_tag_filter_can_combine_with_metadata_tags(
    client,
    account_user,
):
    headers = _auth_headers(client, account_user)
    library = MediaLibrary.create(
        name="movie-tag-combined",
        provider_key="test",
        provider_config={},
    )
    media_tag = MediaTag.create(
        name="media-filter",
        include_terms=["keep"],
        exclude_terms=[],
    )
    metadata_tag = Tag.create(name="metadata-filter")
    expected = _movie("COMBO-001")
    wrong_media_tag = _movie("COMBO-002")
    wrong_metadata_tag = _movie("COMBO-003")
    expected_media = Media.create(
        movie=expected,
        library=library,
        file_name="COMBO-001.mp4",
        release_title="COMBO-001 1080p",
        release_description="source description",
        source_indexer="indexer-a",
        info_hash="0123456789abcdef0123456789abcdef01234567",
        download_task_id=42,
    )
    MediaMediaTag.create(media=expected_media, tag=media_tag)
    MovieTag.create(movie=expected, tag=metadata_tag)
    for movie, has_media_tag, has_metadata_tag in (
        (wrong_media_tag, False, True),
        (wrong_metadata_tag, True, False),
    ):
        media = Media.create(movie=movie, library=library, file_name=f"{movie.movie_number}.mp4")
        if has_media_tag:
            MediaMediaTag.create(media=media, tag=media_tag)
        if has_metadata_tag:
            MovieTag.create(movie=movie, tag=metadata_tag)

    response = client.get(
        "/movies",
        headers=headers,
        params={
            "tag_ids": str(metadata_tag.id),
            "media_tag_ids": str(media_tag.id),
        },
    )

    assert _numbers(response) == {"COMBO-001"}
    assert response.json()["items"][0]["media_items"][0]["tags"] == [
        {"id": media_tag.id, "name": "media-filter"}
    ]
    media_item = response.json()["items"][0]["media_items"][0]
    assert {
        "release_title",
        "release_description",
        "source_indexer",
        "info_hash",
        "download_task_id",
    }.isdisjoint(media_item)


def test_movie_media_tag_filter_rejects_invalid_csv(client, account_user):
    headers = _auth_headers(client, account_user)

    response = client.get(
        "/movies",
        headers=headers,
        params={"media_tag_ids": "1,invalid"},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_movie_filter"
