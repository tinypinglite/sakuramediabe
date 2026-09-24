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
from src.service.playback.media_clip_service import MediaClipService


def _create_clip(
    tmp_path: Path,
    index: int,
    *,
    movie_number: str,
    title: str,
) -> MediaClip:
    library = MediaLibrary.get_or_create(
        name="clip-filter-library",
        defaults={"provider_key": "demo", "provider_config": {}},
    )[0]
    movie = Movie.create(
        movie_number=movie_number,
        javdb_id=f"clip-filter-{index}",
        title=title or movie_number,
    )
    media = Media.create(movie=movie, library=library, file_name=f"clip-{index}.mp4")
    Image.create(
        origin=f"clip-filter-{index}.webp",
        small=f"clip-filter-{index}.webp",
        medium=f"clip-filter-{index}.webp",
        large=f"clip-filter-{index}.webp",
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


def test_keyword_matches_movie_number_and_title(test_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path))
    first = _create_clip(tmp_path, 1, movie_number="CLIPFILTER-001", title="雨夜天台")
    _create_clip(tmp_path, 2, movie_number="CLIPFILTER-002", title="晴天")

    by_number = MediaClipService.list_media_clips(keyword="clipfilter001")
    by_title = MediaClipService.list_media_clips(keyword="天台")
    no_match = MediaClipService.list_media_clips(keyword="不存在")

    assert [item.clip_id for item in by_number.items] == [first.id]
    assert [item.clip_id for item in by_title.items] == [first.id]
    assert no_match.total == 0


def test_keyword_title_match_is_case_insensitive(test_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path))
    clip = _create_clip(tmp_path, 1, movie_number="CLIPCASE-001", title="Rainy Night")

    page = MediaClipService.list_media_clips(keyword="rainy")

    assert [item.clip_id for item in page.items] == [clip.id]


def test_exclude_collection_filters_clips(test_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path))
    first = _create_clip(tmp_path, 1, movie_number="CLIPFILTER-001", title="雨夜天台")
    second = _create_clip(tmp_path, 2, movie_number="CLIPFILTER-002", title="晴天")
    third = _create_clip(tmp_path, 3, movie_number="CLIPFILTER-003", title="早晨")
    collection = ClipCollection.create(name="切片过滤合集", description="")
    ClipCollectionItem.create(collection=collection, clip=first, position=0)

    page = MediaClipService.list_media_clips(exclude_collection_id=collection.id)

    assert {item.clip_id for item in page.items} == {second.id, third.id}
    assert page.total == 2
