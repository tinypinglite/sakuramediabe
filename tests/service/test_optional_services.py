from unittest.mock import Mock

import pytest
import toml

from src.api.exception.errors import ApiError
from src.config.config import (
    ImageSearch,
    Qdrant,
    Settings,
    initialize_optional_services,
    settings,
)
from src.service.system.optional_services import (
    capabilities,
    job_disabled_reason,
    require_image_search,
)


@pytest.mark.parametrize('existing', [False, True])
def test_initialization_distinguishes_existing_database_and_preserves_choices(tmp_path, monkeypatch, existing):
    path = tmp_path / 'config.toml'
    path.write_text('[logging]\nlevel="DEBUG"\n')
    monkeypatch.setitem(Settings.model_config, 'toml_file', path)
    monkeypatch.setattr(settings, 'qdrant', Qdrant())
    monkeypatch.setattr(settings, 'image_search', ImageSearch())
    initialize_optional_services(existing_deployment=existing)
    values = toml.load(path)
    assert values['qdrant']['enabled'] is existing
    assert values['image_search']['enabled'] is existing
    assert values['logging']['level'] == 'DEBUG'
    values['qdrant']['enabled'] = False
    values['image_search']['enabled'] = True
    path.write_text(toml.dumps(values))
    initialize_optional_services(existing_deployment=not existing)
    assert toml.load(path) == values


def test_initialization_leaves_missing_config_to_runtime_bootstrap(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    monkeypatch.setitem(Settings.model_config, 'toml_file', path)
    monkeypatch.setattr(settings, 'qdrant', Qdrant())
    monkeypatch.setattr(settings, 'image_search', ImageSearch())
    initialize_optional_services(existing_deployment=False)
    assert not path.exists()


def test_initialization_writes_missing_config_for_existing_deployment(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    monkeypatch.setitem(Settings.model_config, 'toml_file', path)
    monkeypatch.setattr(settings, 'qdrant', Qdrant())
    monkeypatch.setattr(settings, 'image_search', ImageSearch())
    initialize_optional_services(existing_deployment=True)
    values = toml.load(path)
    assert values['qdrant']['enabled'] is True
    assert values['image_search']['enabled'] is True


def test_runtime_bootstrap_writes_disabled_switches_for_new_deployment(tmp_path, monkeypatch):
    from src.config.config import ensure_runtime_config

    path = tmp_path / 'config.toml'
    monkeypatch.setitem(Settings.model_config, 'toml_file', path)
    monkeypatch.setattr(settings.auth, 'secret_key', 'test-secret')
    monkeypatch.setattr(settings.auth, 'file_signature_secret', 'test-file-secret')
    assert ensure_runtime_config() is True
    values = toml.load(path)
    assert values['qdrant']['enabled'] is False
    assert values['image_search']['enabled'] is False


def test_runtime_bootstrap_keeps_optional_services_for_existing_deployment(tmp_path, monkeypatch):
    from src.config.config import ensure_runtime_config

    path = tmp_path / 'config.toml'
    monkeypatch.setitem(Settings.model_config, 'toml_file', path)
    monkeypatch.setattr(settings.auth, 'secret_key', 'test-secret')
    monkeypatch.setattr(settings.auth, 'file_signature_secret', 'test-file-secret')
    assert ensure_runtime_config(existing_deployment=True) is True
    values = toml.load(path)
    assert values['qdrant']['enabled'] is True
    assert values['image_search']['enabled'] is True


@pytest.mark.parametrize('qdrant,image,expected', [(False, False, False), (True, False, False), (True, True, True), (False, True, False)])
def test_effective_capabilities(monkeypatch, qdrant, image, expected):
    monkeypatch.setattr(settings.qdrant, 'enabled', qdrant)
    monkeypatch.setattr(settings.image_search, 'enabled', image)
    assert capabilities() == {'movie_similarity': qdrant, 'image_search': expected}
    assert bool(job_disabled_reason('image_search_index')) is not expected
    assert bool(job_disabled_reason('movie_similarity_recompute')) is not qdrant
    assert job_disabled_reason('media_thumbnail') is None
    if not expected:
        with pytest.raises(ApiError):
            require_image_search()


def test_disabled_status_and_recommendations_never_probe(monkeypatch):
    from src.service.discovery.moment_recommendation_service import (
        MomentRecommendationService,
    )
    from src.service.discovery.recommendation_service import MovieRecommendationService
    from src.service.system.status_service import StatusService
    monkeypatch.setattr(settings.qdrant, 'enabled', False)
    monkeypatch.setattr(settings.image_search, 'enabled', False)
    embedding = Mock(side_effect=AssertionError('unexpected probe'))
    monkeypatch.setattr(StatusService, '_probe_embedding_service', embedding)
    monkeypatch.setattr(StatusService, '_probe_image_search_vector_store', embedding)
    status = StatusService.get_image_search_status()
    assert status.enabled is False
    store = Mock()
    assert MovieRecommendationService(store=store).search_similar_movies([1]) == {1: []}
    store.search_many.assert_not_called()
    service = MomentRecommendationService(store=store, embedder=Mock())
    assert service._collect_visual_candidates([Mock()], {}) == 0
    service.embedder.embed_images.assert_not_called()


def test_disabled_jobs_never_queue_or_bootstrap(monkeypatch):
    from src.scheduler.registry import JOB_REGISTRY_BY_KEY
    from src.start.aps import _bootstrap_movie_similarity_index, submit_manual_job
    monkeypatch.setattr(settings.qdrant, 'enabled', False)
    monkeypatch.setattr(settings.image_search, 'enabled', False)
    monkeypatch.setattr('src.start.aps.ensure_database_ready', lambda: None)
    enqueue = Mock(side_effect=AssertionError('unexpected enqueue'))
    monkeypatch.setattr('src.start.aps.TaskQueueService.enqueue', enqueue)
    for name in ['image_search_index', 'movie_similarity_recompute']:
        with pytest.raises(ApiError):
            submit_manual_job(JOB_REGISTRY_BY_KEY[name])
    _bootstrap_movie_similarity_index(Mock())
    enqueue.assert_not_called()


def test_saving_optional_services_requires_restart_and_keeps_runtime(monkeypatch, tmp_path):
    from src.service.system.config_service import ConfigService
    path = tmp_path / 'config.toml'
    path.write_text('[qdrant]\nenabled=false\n[image_search]\nenabled=false\n')
    monkeypatch.setitem(Settings.model_config, 'toml_file', path)
    monkeypatch.setattr(settings.qdrant, 'enabled', False)
    monkeypatch.setattr(settings.image_search, 'enabled', False)
    with pytest.raises(ApiError):
        ConfigService.update_config({'image_search': {'enabled': True}})
    result = ConfigService.update_config({'qdrant': {'enabled': True}, 'image_search': {'enabled': True}})
    assert result.restart_required == ['api', 'aps']
    assert toml.load(path)['qdrant']['enabled'] is True
    assert settings.qdrant.enabled is False


def test_disabled_queued_job_completes_without_handler(monkeypatch):
    from src.scheduler.worker import TaskWorker
    from src.service.system.activity import ActivityService
    monkeypatch.setattr(settings.image_search, "enabled", False)
    complete = Mock()
    monkeypatch.setattr(ActivityService, "complete_task_run", complete)
    TaskWorker()._execute(Mock(task_key="image_search_index", id=123))
    assert complete.call_args.kwargs["result_summary"]["skipped"] is True
    assert complete.call_args.kwargs["notify_result"] is False


def test_disabled_reset_does_not_touch_services(monkeypatch):
    from src.service.discovery.image_search_reset_service import ImageSearchResetService
    monkeypatch.setattr(settings.image_search, "enabled", False)
    store, embedder = Mock(), Mock()
    with pytest.raises(ApiError):
        ImageSearchResetService.reset()
    assert not store.mock_calls and not embedder.mock_calls
