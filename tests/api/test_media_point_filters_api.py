from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaThumbnail,
    MomentCollection,
    MomentCollectionItem,
    Movie,
    VideoItem,
)


def _headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _library() -> MediaLibrary:
    return MediaLibrary.get_or_create(
        name="point-filter-api-library",
        defaults={"provider_key": "demo", "provider_config": {}},
    )[0]


def _create_point(media: Media, index: int) -> MediaPoint:
    image = Image.create(
        origin=f"point-filter-api-{index}.webp",
        small=f"point-filter-api-{index}.webp",
        medium=f"point-filter-api-{index}.webp",
        large=f"point-filter-api-{index}.webp",
    )
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=index * 10)
    return MediaPoint.create(
        media=media,
        thumbnail=thumbnail,
        image=image,
        movie_number=media.movie_number,
        video_item_id=media.video_item_id,
        offset_seconds=index * 10,
    )


def _create_jav_point(index: int, movie_number: str) -> MediaPoint:
    movie = Movie.create(
        movie_number=movie_number,
        javdb_id=f"point-filter-api-{index}",
        title=f"Point filter api {index}",
    )
    media = Media.create(
        movie=movie, library=_library(), file_name=f"point-api-{index}.mp4"
    )
    return _create_point(media, index)


def _create_video_point(index: int, title: str) -> MediaPoint:
    video = VideoItem.create(title=title)
    media = Media.create(
        video_item=video, library=_library(), file_name=f"video-api-{index}.mp4"
    )
    return _create_point(media, index)


def test_media_points_keyword_and_kind(client, account_user, test_db):
    headers = _headers(client, account_user)
    _create_jav_point(1, "APIFILTER-001")
    video_point = _create_video_point(2, "海边日落")

    response = client.get(
        "/media-points",
        params={"kind": "all", "keyword": "日落"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["point_id"] for item in payload["items"]] == [video_point.id]
    assert payload["total"] == 1


def test_media_points_exclude_collection(client, account_user, test_db):
    headers = _headers(client, account_user)
    member = _create_jav_point(1, "APIFILTER-001")
    outsider = _create_jav_point(2, "APIFILTER-002")
    collection = MomentCollection.create(name="API 过滤合集", description="")
    MomentCollectionItem.create(collection=collection, point=member, position=0)

    response = client.get(
        "/media-points",
        params={"kind": "jav", "exclude_collection_id": collection.id},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert [item["point_id"] for item in payload["items"]] == [outsider.id]
    assert payload["total"] == 1


def test_media_points_keyword_over_limit(client, account_user, test_db):
    headers = _headers(client, account_user)

    response = client.get(
        "/media-points",
        params={"keyword": "x" * 200},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_media_point_filter"


def test_media_points_invalid_page_size(client, account_user, test_db):
    headers = _headers(client, account_user)

    response = client.get("/media-points", params={"page_size": 101}, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_media_point_filter"
