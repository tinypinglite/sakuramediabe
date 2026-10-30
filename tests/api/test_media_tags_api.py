from src.model import (
    Media,
    MediaLibrary,
    MediaMediaTag,
    MediaTag,
    Movie,
    VideoItem,
)


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


def test_media_tag_manual_assignment_takeover_reset_and_media_list_fields(
    client,
    account_user,
):
    headers = _auth_headers(client, account_user)
    library = MediaLibrary.create(
        name="tag-assignment-api",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="TAG-ASSIGN-001",
        javdb_id="tag-assign-001",
        title="assignment",
    )
    media = Media.create(
        movie=movie,
        library=library,
        file_name="TAG-ASSIGN-001.4k.mp4",
    )
    first = MediaTag.create(
        name="first",
        include_terms=["first"],
        exclude_terms=[],
    )
    second = MediaTag.create(
        name="second",
        include_terms=["TAG-ASSIGN-001"],
        exclude_terms=[],
    )

    initial = client.get(f"/media/{media.id}/tags", headers=headers)
    assert initial.status_code == 200
    assert initial.json() == {
        "media_id": media.id,
        "tags": [],
        "is_media_tags_manually_managed": False,
    }

    assigned = client.put(
        f"/media/{media.id}/tags",
        headers=headers,
        json={"tag_ids": [first.id, second.id]},
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json() == {
        "media_id": media.id,
        "tags": [
            {"id": first.id, "name": "first"},
            {"id": second.id, "name": "second"},
        ],
        "is_media_tags_manually_managed": True,
    }

    listed = client.get(
        "/media",
        headers=headers,
        params={"kind": "jav"},
    )
    assert listed.status_code == 200
    listed_item = next(
        item for item in listed.json()["items"] if item["id"] == media.id
    )
    assert listed_item["tags"] == assigned.json()["tags"]
    assert listed_item["is_media_tags_manually_managed"] is True

    reset = client.post(
        f"/media/{media.id}/tags/reset",
        headers=headers,
    )
    assert reset.status_code == 200, reset.text
    assert reset.json() == {
        "media_id": media.id,
        "tags": [{"id": second.id, "name": "second"}],
        "is_media_tags_manually_managed": False,
    }


def test_media_tag_assignment_rejects_video_missing_and_invalid_ids(
    client,
    account_user,
):
    headers = _auth_headers(client, account_user)
    library = MediaLibrary.create(
        name="tag-assignment-invalid",
        provider_key="test",
        provider_config={},
    )
    video = VideoItem.create(title="video")
    media = Media.create(
        video_item=video,
        library=library,
        file_name="video.mp4",
    )

    unsupported = client.put(
        f"/media/{media.id}/tags",
        headers=headers,
        json={"tag_ids": []},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["code"] == "media_tag_not_supported"

    missing_media = client.get("/media/999999/tags", headers=headers)
    assert missing_media.status_code == 404
    assert missing_media.json()["error"]["code"] == "media_not_found"

    movie = Movie.create(
        movie_number="TAG-ASSIGN-002",
        javdb_id="tag-assign-002",
        title="invalid",
    )
    jav_media = Media.create(
        movie=movie,
        library=library,
        file_name="TAG-ASSIGN-002.mp4",
    )
    missing_tag = client.put(
        f"/media/{jav_media.id}/tags",
        headers=headers,
        json={"tag_ids": [999999]},
    )
    assert missing_tag.status_code == 404
    assert missing_tag.json()["error"]["code"] == "media_tag_not_found"

    invalid_ids = client.put(
        f"/media/{jav_media.id}/tags",
        headers=headers,
        json={"tag_ids": [0]},
    )
    assert invalid_ids.status_code == 422
    assert invalid_ids.json()["error"]["code"] == "validation_error"

    boolean_ids = client.put(
        f"/media/{jav_media.id}/tags",
        headers=headers,
        json={"tag_ids": [True]},
    )
    assert boolean_ids.status_code == 422
    assert boolean_ids.json()["error"]["code"] == "validation_error"

    duplicated_ids = client.put(
        f"/media/{jav_media.id}/tags",
        headers=headers,
        json={"tag_ids": [1, 1]},
    )
    assert duplicated_ids.status_code == 422
    assert duplicated_ids.json()["error"]["code"] == "validation_error"

    unauthorized = client.get(f"/media/{jav_media.id}/tags")
    assert unauthorized.status_code == 401


def test_media_tag_assignment_rejects_invalid_media(
    client,
    account_user,
):
    headers = _auth_headers(client, account_user)
    library = MediaLibrary.create(
        name="tag-assignment-invalid-media",
        provider_key="test",
        provider_config={},
    )
    movie = Movie.create(
        movie_number="TAG-ASSIGN-003",
        javdb_id="tag-assign-003",
        title="invalid media",
    )
    media = Media.create(
        movie=movie,
        library=library,
        file_name="TAG-ASSIGN-003.mp4",
        valid=False,
    )
    tag = MediaTag.create(
        name="invalid-media-tag",
        include_terms=["never"],
        exclude_terms=[],
    )

    response = client.put(
        f"/media/{media.id}/tags",
        headers=headers,
        json={"tag_ids": [tag.id]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_tag_assignment_invalid"

    read = client.get(f"/media/{media.id}/tags", headers=headers)
    assert read.status_code == 422
    assert read.json()["error"]["code"] == "media_tag_assignment_invalid"
