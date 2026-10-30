from src.model import Media, MediaLibrary, Movie, VideoItem


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_media_list_search_matches_filename_across_kinds(
    client,
    account_user,
):
    first_library = MediaLibrary.create(
        name="search-jav-library",
        provider_key="test",
        provider_config={},
    )
    second_library = MediaLibrary.create(
        name="search-video-library",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="SEARCH-JAV-001",
        javdb_id="search-jav-001",
        title="Search JAV",
    )
    video = VideoItem.create(title="Search video")
    jav_media = Media.create(
        movie=movie,
        library=first_library,
        file_name="Search.Mixed.Case-001.MKV",
    )
    video_media = Media.create(
        video_item=video,
        library=second_library,
        file_name="search.mixed.case-002.mp4",
    )
    Media.create(
        movie=movie,
        library=first_library,
        file_name="unrelated.mp4",
    )

    response = client.get(
        "/media",
        params={"search": "  SEARCH.MIXED.CASE  ", "page_size": 1},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] in {jav_media.id, video_media.id}
    assert body["items"][0]["file_name"].lower().startswith("search.mixed.case")


def test_media_list_search_is_filename_only_and_combines_filters(
    client,
    account_user,
):
    jav_library = MediaLibrary.create(
        name="search-filter-jav",
        provider_key="test",
        provider_config={},
    )
    video_library = MediaLibrary.create(
        name="search-filter-video",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="FILENAME-ONLY-001",
        javdb_id="filename-only-001",
        title="Title Needle",
    )
    video = VideoItem.create(title="Title Needle Video")
    jav_media = Media.create(
        movie=movie,
        library=jav_library,
        file_name="movie-release.mkv",
    )
    video_media = Media.create(
        video_item=video,
        library=video_library,
        file_name="Title-Needle-Video.mp4",
    )
    headers = _auth_headers(client, account_user.username)

    filename_only = client.get(
        "/media",
        params={"search": "FILENAME-ONLY"},
        headers=headers,
    )
    combined_jav = client.get(
        "/media",
        params={"search": "MOVIE-RELEASE", "library_id": jav_library.id},
        headers=headers,
    )
    combined_video = client.get(
        "/media",
        params={"search": "TITLE-NEEDLE", "kind": "video"},
        headers=headers,
    )
    blank_search = client.get(
        "/media",
        params={"search": "   "},
        headers=headers,
    )

    assert filename_only.status_code == 200
    assert filename_only.json()["items"] == []
    assert combined_jav.status_code == 200
    assert [item["id"] for item in combined_jav.json()["items"]] == [jav_media.id]
    assert combined_video.status_code == 200
    assert [item["id"] for item in combined_video.json()["items"]] == [video_media.id]
    assert blank_search.status_code == 200
    assert blank_search.json()["total"] == 2
