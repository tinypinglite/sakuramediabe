from src.model import ClipCollection, ClipCollectionItem, MediaClip
from src.service.collections.clip_collection_service import ClipCollectionService


def test_clip_collection_list_omits_empty_clip_placeholder(test_db):
    clip = MediaClip.create(
        movie_number="COLLECTION-001",
        start_offset_seconds=0,
        end_offset_seconds=10,
        file_path="",
        file_size_bytes=0,
        duration_seconds=0,
    )
    collection = ClipCollection.create(name="empty-clip-collection", description="")
    ClipCollectionItem.create(collection=collection, clip=clip, position=0)

    result = ClipCollectionService.list_collection_clips(collection.id)
    summary = ClipCollectionService.list_collections()
    detail = ClipCollectionService.get_collection(collection.id)

    assert result.total == 0
    assert result.items == []
    assert summary[0].clip_count == 0
    assert summary[0].cover_image is None
    assert detail.clip_count == 0
    assert detail.cover_image is None
    assert MediaClip.get_or_none(MediaClip.id == clip.id) is None
