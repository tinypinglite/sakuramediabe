from types import SimpleNamespace

import pytest
from PIL import Image as PILImage

from src.config.config import settings
from src.model import (
    ClipCollection,
    ClipCollectionItem,
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaThumbnail,
    MomentCollection,
    MomentCollectionItem,
    Movie,
    VideoItem,
)
from src.plugins.provider_protocol import MEDIA_PROVIDER_REGISTRY
from src.service.discovery.moment_recommendation_service import (
    MomentRecommendationService,
)
from src.service.playback import media_service


@pytest.mark.parametrize(('kind', 'delete_video'), [('jav', False), ('video', False), ('video', True)])
def test_deleted_source_preserves_points_images_collections_and_clips(
    client, account_user, monkeypatch, tmp_path, kind, delete_video,
):
    image_root = tmp_path / 'images'
    image_root.mkdir()
    clip_root = tmp_path / 'clips'
    clip_root.mkdir()
    monkeypatch.setattr(settings.media, 'import_image_root_path', str(image_root))
    monkeypatch.setattr('src.service.playback.media_clip_service.media_clip_root_path', lambda: clip_root)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, 'storage_for',
        lambda _: SimpleNamespace(delete_media=lambda **kwargs: None),
    )
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, 'require',
        lambda _: SimpleNamespace(playback_deliveries=('proxy',)),
    )
    monkeypatch.setattr(
        media_service, 'get_qdrant_thumbnail_store',
        lambda: SimpleNamespace(delete_by_media_id=lambda _: None),
    )
    login = client.post('/auth/tokens', json={'username': account_user.username, 'password': 'password123'})
    headers = {'Authorization': f"Bearer {login.json()['access_token']}"}
    library = MediaLibrary.create(name='preservation', provider_key='demo', provider_config={})
    owner = (
        {'movie': Movie.create(movie_number='KEEP-001', title='JAV')}
        if kind == 'jav' else {'video_item': VideoItem.create(title='Video')}
    )
    media = Media.create(library=library, file_name='source.mp4', **owner)
    # 同影片/视频仍有另一版本时，旧时刻也不能自动改绑。
    other_media = Media.create(library=library, file_name='other.mp4', **owner)
    thumbnails = []
    for offset in (10, 20, 30):
        path = image_root / f'{offset}.webp'
        PILImage.new('RGB', (20, 20), 'blue').save(path)
        image = Image.create(origin=path.name, small=path.name, medium=path.name, large=path.name)
        thumbnails.append(MediaThumbnail.create(media=media, image=image, offset=offset))
    point_ids = []
    for thumbnail in thumbnails[:2]:
        response = client.post(f'/media/{media.id}/points', headers=headers, json={'thumbnail_id': thumbnail.id})
        assert response.status_code == 201, response.text
        point_ids.append(response.json()['point_id'])
    repeated = client.post(f'/media/{media.id}/points', headers=headers, json={'thumbnail_id': thumbnails[0].id})
    assert repeated.status_code == 200 and repeated.json()['point_id'] == point_ids[0]
    detail_path = f'/movies/{media.movie_number}' if kind == 'jav' else f'/videos/{media.video_item_id}'
    detail = client.get(detail_path, headers=headers)
    assert detail.status_code == 200, detail.text
    source_detail = next(item for item in detail.json()['media_items'] if item['media_id'] == media.id)
    assert [point['point_id'] for point in source_detail['points']] == point_ids
    assert client.get(source_detail['points'][0]['image']['origin']).status_code == 200
    original = MediaPoint.get_by_id(point_ids[0])
    assert original.movie_number == media.movie_number
    assert original.video_item_id == media.video_item_id
    collection = MomentCollection.create(name='saved')
    for position, point_id in enumerate(reversed(point_ids)):
        MomentCollectionItem.create(collection=collection, point=point_id, position=position)
    if kind == 'video':
        video = owner['video_item']
        video.cover_image = thumbnails[0].image_id
        video.save()
    clip_path = clip_root / 'clip.mp4'
    clip_path.write_bytes(b'test video bytes')
    clip = MediaClip.create(
        media=media, movie_number=media.movie_number, start_offset_seconds=10, end_offset_seconds=20,
        file_path='clip.mp4', file_size_bytes=clip_path.stat().st_size, duration_seconds=10,
    )
    clip_collection = ClipCollection.create(name='clips')
    ClipCollectionItem.create(collection=clip_collection, clip=clip, position=0)

    response = client.delete(
        f'/videos/{media.video_item_id}' if delete_video else f'/media/{media.id}', headers=headers,
    )
    assert response.status_code == 204, response.text
    assert Media.get_or_none(Media.id == media.id) is None
    assert bool(Media.get_or_none(Media.id == other_media.id)) is (not delete_video)
    for point_id in point_ids:
        point = MediaPoint.get_by_id(point_id)
        assert point.media_id is None and point.thumbnail_id is None
    assert MediaPoint.get_by_id(point_ids[0]).created_at == original.created_at
    assert (image_root / '10.webp').is_file() and (image_root / '20.webp').is_file()
    assert not (image_root / '30.webp').exists()
    assert MomentRecommendationService._load_seeds() == []

    for filter_kind, total in [(kind, 2), ('all', 2), ('video' if kind == 'jav' else 'jav', 0)]:
        response = client.get('/media-points', params={'kind': filter_kind, 'page_size': 1}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['total'] == total
        assert len(response.json()['items']) == min(total, 1)
    item = client.get('/media-points', params={'kind': kind}, headers=headers).json()['items'][0]
    assert item['media_id'] is None and item['thumbnail_id'] is None
    assert item['movie_number'] == media.movie_number and item['video_item_id'] == media.video_item_id
    assert client.get(item['image']['origin']).status_code == 200
    # 媒体列表的分类查询仍基于 Media，不能被时刻快照筛选改坏。
    listing = client.get('/media', params={'kind': kind}, headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()['total'] == (0 if delete_video else 1)
    summary = client.get(f'/moment-collections/{collection.id}', headers=headers)
    assert summary.status_code == 200, summary.text
    assert summary.json()['point_count'] == 2 and summary.json()['cover_image'] is not None
    members = client.get(f'/moment-collections/{collection.id}/points', headers=headers)
    assert members.status_code == 200, members.text
    assert [item['point_id'] for item in members.json()['items']] == list(reversed(point_ids))
    clip_listing = client.get('/media-clips', headers=headers)
    assert clip_listing.status_code == 200, clip_listing.text
    saved_clip = clip_listing.json()['items'][0]
    assert saved_clip['media_id'] is None and saved_clip['cover_image'] is None
    assert client.get(saved_clip['stream_url']).content == clip_path.read_bytes()
    assert ClipCollectionItem.select().where(ClipCollectionItem.clip == clip).exists()

    assert client.delete(f'/media-points/{point_ids[1]}').status_code == 401
    assert MediaPoint.get_or_none(MediaPoint.id == point_ids[1]) is not None
    for point_id in point_ids:
        assert client.delete(f'/media-points/{point_id}', headers=headers).status_code == 204
    assert not MomentCollectionItem.select().where(MomentCollectionItem.collection == collection).exists()
    assert not (image_root / '20.webp').exists()
    if kind == 'video' and not delete_video:
        # 封面仍在引用，直到 VideoItem 也被删除才能清理。
        assert (image_root / '10.webp').exists()
        assert client.delete(f'/videos/{media.video_item_id}', headers=headers).status_code == 204
    assert not (image_root / '10.webp').exists()
    assert client.delete(f'/media-points/{point_ids[0]}', headers=headers).status_code == 404
    assert clip_path.is_file()


def test_replacing_video_cover_keeps_image_referenced_by_orphan_point(
    client, account_user, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings.media, 'import_image_root_path', str(tmp_path))
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, 'require',
        lambda _: SimpleNamespace(playback_deliveries=('proxy',)),
    )
    old_path = tmp_path / 'old.webp'
    PILImage.new('RGB', (20, 20), 'blue').save(old_path)
    old_image = Image.create(origin=old_path.name, small=old_path.name, medium=old_path.name, large=old_path.name)
    video = VideoItem.create(title='video', cover_image=old_image)
    point = MediaPoint.create(image=old_image, video_item_id=video.id, offset_seconds=10)
    library = MediaLibrary.create(name='covers', provider_key='demo', provider_config={})
    media = Media.create(video_item=video, library=library, file_name='other.mp4')
    new_image = Image.create(origin='new.webp', small='new.webp', medium='new.webp', large='new.webp')
    thumbnail = MediaThumbnail.create(media=media, image=new_image, offset=20)
    login = client.post('/auth/tokens', json={'username': account_user.username, 'password': 'password123'})
    headers = {'Authorization': f"Bearer {login.json()['access_token']}"}

    response = client.patch(f'/videos/{video.id}', headers=headers, json={'cover_thumbnail_id': thumbnail.id})

    assert response.status_code == 200, response.text
    assert VideoItem.get_by_id(video.id).cover_image_id == new_image.id
    assert old_path.is_file() and Image.get_or_none(Image.id == old_image.id) is not None
    assert client.delete(f'/media-points/{point.id}', headers=headers).status_code == 204
    assert not old_path.exists()
    assert Image.get_or_none(Image.id == new_image.id) is not None
