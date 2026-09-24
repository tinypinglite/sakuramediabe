from pathlib import Path

from src.config.config import settings
from src.model import (
    ClipCollection,
    ClipCollectionItem,
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    Movie,
)


def _headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_clip(
    tmp_path: Path,
    index: int,
    *,
    movie_number: str,
    title: str,
) -> MediaClip:
    library = MediaLibrary.get_or_create(
        name="clip-filter-api-library",
        defaults={"provider_key": "demo", "provider_config": {}},
    )[0]
    movie = Movie.create(
        movie_number=movie_number,
        javdb_id=f"clip-filter-api-{index}",
        title=title or movie_number,
    )
    media = Media.create(movie=movie, library=library, file_name=f"clip-api-{index}.mp4")
    Image.create(
        origin=f"clip-filter-api-{index}.webp",
        small=f"clip-filter-api-{index}.webp",
        medium=f"clip-filter-api-{index}.webp",
        large=f"clip-filter-api-{index}.webp",
    )
    payload = b"valid clip"
    relative_path = f"{movie_number}/{index}.mp4"
    clip_path = tmp_path / relative_path
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(payload)
    return MediaClip.create(
        media=media,
        movie_number=movie_number,
        start_offset_seconds=index * 10,
        end_offset_seconds=index * 10 + 10,
        title=title,
        file_path=relative_path,
        file_size_bytes=len(payload),
        duration_seconds=10,
    )


def test_media_clips_keyword_and_exclude(
    client, account_user, test_db, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path))
    headers = _headers(client, account_user)
    member = _create_clip(
        tmp_path, 1, movie_number="APICLIPFILTER-001", title="雨夜天台"
    )
    outsider = _create_clip(
        tmp_path, 2, movie_number="APICLIPFILTER-002", title="晴天"
    )
    collection = ClipCollection.create(name="API 切片过滤合集", description="")
    ClipCollectionItem.create(collection=collection, clip=member, position=0)

    by_title = client.get(
        "/media-clips", params={"keyword": "天台"}, headers=headers
    )
    assert by_title.status_code == 200
    assert [item["clip_id"] for item in by_title.json()["items"]] == [member.id]

    excluded = client.get(
        "/media-clips",
        params={"exclude_collection_id": collection.id},
        headers=headers,
    )
    assert excluded.status_code == 200
    assert [item["clip_id"] for item in excluded.json()["items"]] == [outsider.id]
    assert excluded.json()["total"] == 1


def test_media_clips_invalid_page_size(client, account_user, test_db):
    headers = _headers(client, account_user)

    response = client.get("/media-clips", params={"page_size": 101}, headers=headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_media_clip_filter"
