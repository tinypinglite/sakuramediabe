from pathlib import Path

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import (
    ClipCollection,
    ClipCollectionItem,
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    MediaThumbnail,
    Movie,
    MovieSeries,
)
from src.schema.collections.clips import (
    ClipCollectionCreateRequest,
    ClipCollectionUpdateRequest,
)
from src.schema.playback.clips import MediaClipCreateRequest
from src.service.collections.clip_collection_service import ClipCollectionService
from src.service.playback.media_clip_service import MediaClipService

CLIP_MODELS = [
    MovieSeries,
    Movie,
    Image,
    MediaLibrary,
    Media,
    MediaThumbnail,
    MediaClip,
    ClipCollection,
    ClipCollectionItem,
]


def _create_media(movie_number: str, tmp_path: Path) -> Media:
    movie = Movie.create(movie_number=movie_number, javdb_id=f"jav-{movie_number}", title=movie_number)
    source = tmp_path / f"{movie_number}.mp4"
    source.write_bytes(b"video-bytes")
    media = Media.create(movie=movie, path=str(source), valid=True)
    for offset in (0, 10, 20, 30):
        path = f"movies/{movie_number}/media/fp/thumbnails/{offset}.webp"
        image = Image.create(origin=path, small=path, medium=path, large=path)
        MediaThumbnail.create(media=media, image=image, offset=offset)
    return media


def _create_clip(media: Media, start: int, end: int) -> int:
    start_id = MediaThumbnail.get(MediaThumbnail.media == media, MediaThumbnail.offset == start).id
    end_id = MediaThumbnail.get(MediaThumbnail.media == media, MediaThumbnail.offset == end).id
    resource, _ = MediaClipService.create_clip(
        media.id,
        MediaClipCreateRequest(start_thumbnail_id=start_id, end_thumbnail_id=end_id),
    )
    return resource.clip_id


@pytest.fixture()
def collection_env(test_db, tmp_path, monkeypatch):
    test_db.bind(CLIP_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(CLIP_MODELS)
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "clips"))
    monkeypatch.setattr(settings.media, "media_clip_max_duration_seconds", 900)
    monkeypatch.setattr(
        MediaClipService,
        "_cut_clip_file",
        staticmethod(lambda source, target, start, end: target.write_bytes(b"clip-bytes")),
    )
    # 两部不同影片的媒体，用于验证跨影片成员。
    media_a = _create_media("ABC-001", tmp_path)
    media_b = _create_media("ABC-002", tmp_path)
    yield {"media_a": media_a, "media_b": media_b}
    test_db.drop_tables(list(reversed(CLIP_MODELS)))


def test_create_collection_and_uniqueness(collection_env):
    created = ClipCollectionService.create_collection(
        ClipCollectionCreateRequest(name=" 我的合集 ", description=" desc ")
    )
    assert created.name == "我的合集"
    assert created.description == "desc"
    assert created.clip_count == 0

    with pytest.raises(ApiError) as exc:
        ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="我的合集"))
    assert exc.value.code == "clip_collection_name_conflict"


def test_add_clip_appends_in_order_across_movies(collection_env):
    env = collection_env
    collection = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="c1"))
    clip_a = _create_clip(env["media_a"], 0, 10)
    clip_b = _create_clip(env["media_b"], 10, 30)

    ClipCollectionService.add_clip(collection.id, clip_a)
    ClipCollectionService.add_clip(collection.id, clip_b)
    # 重复添加应幂等，不新增成员。
    ClipCollectionService.add_clip(collection.id, clip_a)

    page = ClipCollectionService.list_collection_clips(collection.id)
    assert page.total == 2
    assert [item.clip_id for item in page.items] == [clip_a, clip_b]
    assert [item.position for item in page.items] == [0, 1]
    # 跨影片成员：两条来自不同番号。
    assert {item.movie_number for item in page.items} == {"ABC-001", "ABC-002"}


def test_set_clips_replaces_and_reorders(collection_env):
    env = collection_env
    collection = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="c2"))
    clip_a = _create_clip(env["media_a"], 0, 10)
    clip_b = _create_clip(env["media_a"], 10, 20)
    clip_c = _create_clip(env["media_b"], 0, 20)
    ClipCollectionService.add_clip(collection.id, clip_a)
    ClipCollectionService.add_clip(collection.id, clip_b)

    # 全量有序设置：换成 [c, a]，去掉 b。
    ClipCollectionService.set_clips(collection.id, [clip_c, clip_a, clip_c])

    page = ClipCollectionService.list_collection_clips(collection.id)
    assert [item.clip_id for item in page.items] == [clip_c, clip_a]
    assert [item.position for item in page.items] == [0, 1]


def test_same_clip_in_multiple_collections(collection_env):
    env = collection_env
    clip = _create_clip(env["media_a"], 0, 10)
    first = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="first"))
    second = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="second"))

    ClipCollectionService.add_clip(first.id, clip)
    ClipCollectionService.add_clip(second.id, clip)

    assert ClipCollectionService.list_collection_clips(first.id).total == 1
    assert ClipCollectionService.list_collection_clips(second.id).total == 1


def test_remove_clip_from_collection(collection_env):
    env = collection_env
    collection = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="c3"))
    clip = _create_clip(env["media_a"], 0, 10)
    ClipCollectionService.add_clip(collection.id, clip)

    ClipCollectionService.remove_clip(collection.id, clip)

    assert ClipCollectionService.list_collection_clips(collection.id).total == 0
    # 移出合集不删片段本体。
    assert MediaClip.get_or_none(MediaClip.id == clip) is not None


def test_delete_clip_removes_it_from_collections(collection_env):
    env = collection_env
    collection = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="c4"))
    clip = _create_clip(env["media_a"], 0, 10)
    ClipCollectionService.add_clip(collection.id, clip)

    MediaClipService.delete_clip(clip)

    # 片段删除后，合集成员被外键 CASCADE 自动清理，但合集本身保留。
    assert ClipCollectionService.list_collection_clips(collection.id).total == 0
    assert ClipCollection.get_or_none(ClipCollection.id == collection.id) is not None


def test_update_collection_name_and_description(collection_env):
    collection = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="old"))

    updated = ClipCollectionService.update_collection(
        collection.id, ClipCollectionUpdateRequest(name="new", description="d")
    )

    assert updated.name == "new"
    assert updated.description == "d"


def test_collection_cover_is_first_clip_cover(collection_env):
    env = collection_env
    collection = ClipCollectionService.create_collection(ClipCollectionCreateRequest(name="cover"))
    clip = _create_clip(env["media_a"], 10, 30)
    ClipCollectionService.add_clip(collection.id, clip)

    resources = ClipCollectionService.list_collections()
    target = next(item for item in resources if item.id == collection.id)
    assert target.clip_count == 1
    assert target.cover_image is not None
