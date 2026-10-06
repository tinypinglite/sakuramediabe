from pathlib import Path

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
)
from src.service.collections.clip_collection_service import ClipCollectionService


def _create_clip(
    tmp_path: Path,
    index: int,
    *,
    movie_number: str,
    cover: Image | None = None,
) -> MediaClip:
    library = MediaLibrary.get_or_create(
        name="clip-overview-library",
        defaults={"provider_key": "demo", "provider_config": {}},
    )[0]
    movie = Movie.create(
        movie_number=movie_number,
        javdb_id=f"clip-overview-{index}",
        title=movie_number,
    )
    media = Media.create(movie=movie, library=library, file_name=f"clip-overview-{index}.mp4")
    payload = b"valid clip"
    relative_path = f"{movie_number}/{index}.mp4"
    clip_path = tmp_path / relative_path
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(payload)
    clip = MediaClip.create(
        media=media,
        movie_number=movie_number,
        start_offset_seconds=index * 10,
        end_offset_seconds=index * 10 + 10,
        title=movie_number,
        file_path=relative_path,
        file_size_bytes=len(payload),
        duration_seconds=10,
    )
    if cover is not None:
        MediaThumbnail.create(
            media=media, image=cover, offset=clip.start_offset_seconds
        )
    return clip


def test_list_collections_batch_loads_counts_and_covers_in_position_order(
    test_db, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path))
    first_cover = Image.create(origin="clip-cover-first.jpg")
    later_cover = Image.create(origin="clip-cover-later.jpg")
    collection = ClipCollection.create(name="切片系列 A", description="")
    first = _create_clip(tmp_path, 1, movie_number="CLIPOV-001", cover=first_cover)
    later = _create_clip(tmp_path, 2, movie_number="CLIPOV-002", cover=later_cover)
    # position 0 的成员排在前面，封面取它而不是"第一个有封面的成员"。
    ClipCollectionItem.create(collection=collection, clip=later, position=0)
    ClipCollectionItem.create(collection=collection, clip=first, position=1)

    no_cover_clip = _create_clip(tmp_path, 3, movie_number="CLIPOV-003")
    no_cover_collection = ClipCollection.create(name="切片系列 B", description="")
    ClipCollectionItem.create(
        collection=no_cover_collection, clip=no_cover_clip, position=0
    )
    ClipCollection.create(name="空合集", description="")

    resources = {
        resource.name: resource for resource in ClipCollectionService.list_collections()
    }

    assert resources["空合集"].clip_count == 0
    assert resources["空合集"].cover_image is None
    assert resources["切片系列 A"].clip_count == 2
    assert resources["切片系列 A"].cover_image is not None
    # origin 会被签名成 /files/images/... URL，这里断言文件名即可。
    assert "clip-cover-later.jpg" in resources["切片系列 A"].cover_image.origin
    assert resources["切片系列 B"].clip_count == 1
    assert resources["切片系列 B"].cover_image is None

    # 单合集路径与批量路径语义一致。
    detail = ClipCollectionService.get_collection(collection.id)
    assert detail.cover_image is not None
    assert "clip-cover-later.jpg" in detail.cover_image.origin
