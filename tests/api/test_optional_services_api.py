from unittest.mock import Mock

import pytest

from src.config.config import settings
from src.service.system.status_service import StatusService


@pytest.mark.parametrize('qdrant,image', [(False, False), (True, False), (True, True)])
def test_capability_endpoint_only_reads_configuration(client, account_user, monkeypatch, qdrant, image):
    token = client.post('/auth/tokens', json={'username': account_user.username, 'password': 'password123'}).json()['access_token']
    headers = {'Authorization': f'Bearer {token}'}
    monkeypatch.setattr(settings.qdrant, 'enabled', qdrant)
    monkeypatch.setattr(settings.image_search, 'enabled', image)
    probe = Mock(side_effect=AssertionError('unexpected probe'))
    monkeypatch.setattr(StatusService, '_probe_embedding_service', probe)
    monkeypatch.setattr(StatusService, '_probe_image_search_vector_store', probe)
    response = client.get('/status/capabilities', headers=headers)
    assert response.status_code == 200
    assert response.json() == {'movie_similarity': qdrant, 'image_search': qdrant and image}
    if not image:
        status = client.get('/status/image-search', headers=headers)
        assert status.json()['enabled'] is False
        assert client.post('/image-search/text-sessions', data={'text': 'test'}, headers=headers).status_code == 409
        assert client.post('/image-search/reset', headers=headers).status_code == 409
        assert client.post('/system/jobs/image_search_index/run', headers=headers).status_code == 409
    jobs = client.get('/system/jobs', headers=headers).json()
    job = next(item for item in jobs if item['task_key'] == 'image_search_index')
    assert job['manual_trigger_allowed'] is (qdrant and image)
    assert bool(job['disabled_reason']) is not (qdrant and image)
    assert not probe.mock_calls
