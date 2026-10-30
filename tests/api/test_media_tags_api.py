from src.model import Media, MediaLibrary, MediaMediaTag, MediaTag, Movie


def _auth_headers(client, account_user) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_media_tag_crud_and_media_count(client, account_user):
    headers = _auth_headers(client, account_user)

    created = client.post(
        "/media-tags",
        headers=headers,
        json={
            "name": "高画质",
            "include_terms": ["4K", " 4k ", "2160p"],
            "exclude_terms": ["sample"],
            "enabled": True,
        },
    )
    assert created.status_code == 201, created.text
    tag = created.json()
    assert tag["name"] == "高画质"
    assert tag["include_terms"] == ["4K", "2160p"]
    assert tag["media_count"] == 0

    library = MediaLibrary.create(name="tag-api", provider_key="test", provider_config={})
    movie = Movie.create(movie_number="TAG-API-001", title="tag api")
    media = Media.create(movie=movie, library=library, file_name="TAG-API-001.4k.mp4")
    MediaMediaTag.create(media=media, tag=tag["id"])

    listed = client.get(
        "/media-tags",
        headers=headers,
        params={"query": "画", "sort": "media_count:desc"},
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["media_count"] == 1

    updated = client.put(
        f"/media-tags/{tag['id']}",
        headers=headers,
        json={
            "name": "高清",
            "include_terms": ["1080p"],
            "exclude_terms": [],
            "enabled": False,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "高清"
    assert updated.json()["media_count"] == 1

    deleted = client.delete(f"/media-tags/{tag['id']}", headers=headers)
    assert deleted.status_code == 204
    assert MediaTag.get_or_none(MediaTag.id == tag["id"]) is None
    assert not MediaMediaTag.select().exists()


def test_media_tag_name_conflict_and_not_found(client, account_user):
    headers = _auth_headers(client, account_user)
    payload = {
        "name": "Duplicate",
        "include_terms": ["keep"],
        "exclude_terms": [],
        "enabled": True,
    }
    assert client.post("/media-tags", headers=headers, json=payload).status_code == 201

    conflict = client.post("/media-tags", headers=headers, json=payload)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "media_tag_name_conflict"

    missing = client.put("/media-tags/999999", headers=headers, json=payload)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "media_tag_not_found"


def test_media_tag_preview_applies_exclusion_first(client, account_user):
    headers = _auth_headers(client, account_user)
    client.post(
        "/media-tags",
        headers=headers,
        json={
            "name": "4K",
            "include_terms": ["４Ｋ"],
            "exclude_terms": ["sample"],
            "enabled": True,
        },
    )
    client.post(
        "/media-tags",
        headers=headers,
        json={
            "name": "字幕",
            "include_terms": ["中文字幕"],
            "exclude_terms": [],
            "enabled": True,
        },
    )

    matched = client.post(
        "/media-tags/preview",
        headers=headers,
        json={"file_name": "MOVIE.4k.mkv"},
    )
    assert matched.status_code == 200
    assert matched.json()["matches"] == [
        {"tag_id": 1, "name": "4K", "matched_term": "４Ｋ"}
    ]

    excluded = client.post(
        "/media-tags/preview",
        headers=headers,
        json={"file_name": "MOVIE.4k.SAMPLE.mkv"},
    )
    assert excluded.status_code == 200
    assert excluded.json()["matches"] == []

    release_metadata = client.post(
        "/media-tags/preview",
        headers=headers,
        json={
            "file_name": "MOVIE.mkv",
            "release_title": "MOVIE 4K",
            "release_description": "中文字幕",
        },
    )
    assert release_metadata.status_code == 200
    assert {match["name"] for match in release_metadata.json()["matches"]} == {
        "4K",
        "字幕",
    }

    excluded_by_description = client.post(
        "/media-tags/preview",
        headers=headers,
        json={
            "file_name": "MOVIE.mkv",
            "release_title": "MOVIE 4K",
            "release_description": "SAMPLE release",
        },
    )
    assert excluded_by_description.status_code == 200
    assert excluded_by_description.json()["matches"] == []


def test_media_tag_filter_validation_and_authentication(client, account_user):
    headers = _auth_headers(client, account_user)

    invalid_sort = client.get(
        "/media-tags",
        headers=headers,
        params={"sort": "unknown:desc"},
    )
    assert invalid_sort.status_code == 422
    assert invalid_sort.json()["error"]["code"] == "invalid_media_tag_filter"

    invalid_terms = client.post(
        "/media-tags",
        headers=headers,
        json={
            "name": "invalid",
            "include_terms": ["   "],
            "exclude_terms": [],
            "enabled": True,
        },
    )
    assert invalid_terms.status_code == 422

    for field_name, invalid_value in (
        ("include_terms", [123]),
        ("exclude_terms", [None]),
    ):
        payload = {
            "name": "invalid",
            "include_terms": ["keep"],
            "exclude_terms": [],
            "enabled": True,
        }
        payload[field_name] = invalid_value
        response = client.post("/media-tags", headers=headers, json=payload)
        assert response.status_code == 422

    unauthorized = client.get("/media-tags")
    assert unauthorized.status_code == 401
