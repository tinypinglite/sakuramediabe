from datetime import datetime

import pytest

from src.model import Media, MediaLibrary, Movie, VideoItem
from src.service.playback.media_service import MediaService


def _headers(client, user):
    response = client.post('/auth/tokens', json={'username': user.username, 'password': 'password123'})
    return {'Authorization': f"Bearer {response.json()['access_token']}"}


def test_groups_by_jav_movie_across_libraries_without_hash_requirement(client, account_user):
    local = MediaLibrary.create(name='local', provider_key='local')
    cloud = MediaLibrary.create(name='cloud', provider_key='cloud115')
    movie = Movie.create(movie_number='MULTI-001', javdb_id='multi-001', title='Two versions')
    first = Media.create(movie=movie, library=local, file_name='4k.mp4', file_hash='a',
                         resolution='2160p', duration_seconds=7200, file_size_bytes=123456)
    second = Media.create(movie=movie, library=cloud, file_name='hd.mp4', file_hash='b')
    third = Media.create(movie=movie, library=local, file_name='missing.mp4', valid=False)
    single = Movie.create(movie_number='SINGLE-001', javdb_id='single-001', title='Single')
    Media.create(movie=single, library=local, file_hash='a')
    Movie.create(movie_number='EMPTY-001', javdb_id='empty-001', title='Empty')
    video = VideoItem.create(title='Non JAV')
    Media.create(video_item=video, library=local)
    Media.create(video_item=video, library=cloud)

    response = client.get('/media/multi-version-movies', headers=_headers(client, account_user))
    assert response.status_code == 200
    body = response.json()
    assert body['total'] == 1
    group = body['items'][0]
    assert group['movie_number'] == movie.movie_number
    assert group['media_count'] == 3
    assert [item['id'] for item in group['media_items']] == [first.id, second.id, third.id]
    assert {item['library_name'] for item in group['media_items']} == {'local', 'cloud'}
    assert group['media_items'][0]['resolution'] == '2160p'
    assert group['media_items'][0]['duration_seconds'] == 7200
    assert group['media_items'][0]['file_size_bytes'] == 123456
    assert group['media_items'][0]['title'] == 'Two versions'
    assert group['media_items'][2]['valid'] is False


def test_pagination_excludes_vr_numbers_and_returns_all_versions(test_db):
    library = MediaLibrary.create(name='local', provider_key='local')
    timestamp = datetime(2026, 1, 1)
    for number in [
        'MULTI-003', 'MULTI-001', 'MULTI-002',
        'VR-001', 'AAA-VR-001', 'AAA-001vr', 'AAA-Vr-002',
    ]:
        movie = Movie.create(movie_number=number, javdb_id=number, title=number)
        for index in range(3):
            Media.create(movie=movie, library=library, file_name=str(index), updated_at=timestamp)
    first = MediaService.list_multi_version_movies(page_size=1)
    second = MediaService.list_multi_version_movies(page=2, page_size=1)
    assert first.total == second.total == 3
    assert first.items[0].movie_number == 'MULTI-001'
    assert second.items[0].movie_number == 'MULTI-002'
    assert len(second.items[0].media_items) == 3
    Media.delete_by_id(first.items[0].media_items[0].id)
    assert MediaService.list_multi_version_movies().items[0].media_count == 2
    Media.delete_by_id(first.items[0].media_items[1].id)
    after = MediaService.list_multi_version_movies(page_size=1)
    assert after.total == 2
    assert after.items[0].movie_number == 'MULTI-002'
    assert MediaService.list_multi_version_movies(page=4, page_size=1).items == []


def test_requires_authentication(client):
    assert client.get('/media/multi-version-movies').status_code == 401


@pytest.mark.parametrize('params', [{'page': 0}, {'page_size': 0}])
def test_rejects_invalid_pagination(client, account_user, params):
    response = client.get('/media/multi-version-movies', params=params, headers=_headers(client, account_user))
    assert response.status_code == 422
