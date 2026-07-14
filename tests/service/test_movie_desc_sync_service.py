from src.metadata._providers.dmm import (
    DmmMovieDescNotFoundError,
    DmmMovieNumberNotFoundError,
)
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Movie, ResourceTaskState
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.movie_desc_sync_service import MovieDescSyncService
from src.service.system.resource_task_state_service import ResourceTaskStateService


class FakeDmmProvider:
    def __init__(self, desc_by_number=None, failures=None):
        self.desc_by_number = desc_by_number or {}
        self.failures = failures or {}

    def get_movie_desc(self, movie_number: str) -> str:
        if movie_number in self.failures:
            raise self.failures[movie_number]
        return self.desc_by_number[movie_number]


class CountingDmmProvider:
    """按预设结果序列逐次返回，并记录每次被调用的番号，用于校验熔断跳过。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def get_movie_desc(self, movie_number: str) -> str:
        self.calls.append(movie_number)
        outcome = self.results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _request_error():
    return MetadataRequestError("GET", "https://www.dmm.co.jp", "SSL EOF")


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def _create_task_state(movie: Movie, **kwargs) -> ResourceTaskState:
    payload = {
        "task_key": MovieDescSyncService.TASK_KEY,
        "resource_type": "movie",
        "resource_id": movie.id,
    }
    payload.update(kwargs)
    return ResourceTaskState.create(**payload)


def test_movie_desc_sync_service_only_processes_empty_desc(app):
    _create_movie("ABP-001", "MovieA1", desc="")
    _create_movie("ABP-002", "MovieA2", desc="existing")
    service = MovieDescSyncService(
        catalog_import_service=CatalogImportService(
            dmm_provider=FakeDmmProvider(desc_by_number={"ABP-001": "desc 1"})
        )
    )

    stats = service.run()

    assert stats == {
        "candidate_movies": 1,
        "processed_movies": 1,
        "succeeded_movies": 1,
        "failed_movies": 0,
        "updated_movies": 1,
        "skipped_movies": 0,
    }
    assert Movie.get(Movie.movie_number == "ABP-001").desc == "desc 1"
    assert Movie.get(Movie.movie_number == "ABP-002").desc == "existing"


def test_movie_desc_sync_service_counts_failures(app):
    _create_movie("ABP-003", "MovieA3", desc="")
    _create_movie("ABP-004", "MovieA4", desc="")
    service = MovieDescSyncService(
        catalog_import_service=CatalogImportService(
            dmm_provider=FakeDmmProvider(
                desc_by_number={"ABP-003": "desc 3"},
                failures={"ABP-004": MetadataNotFoundError("movie_desc", "ABP-004")},
            )
        )
    )

    stats = service.run()

    assert stats == {
        "candidate_movies": 2,
        "processed_movies": 2,
        "succeeded_movies": 1,
        "failed_movies": 1,
        "updated_movies": 1,
        "skipped_movies": 0,
    }
    failed_movie = Movie.get(Movie.movie_number == "ABP-004")
    failed_state = ResourceTaskState.get(
        ResourceTaskState.task_key == MovieDescSyncService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == failed_movie.id,
    )
    assert failed_movie.desc == ""
    assert failed_state.state == "failed"


def test_movie_desc_sync_service_skips_terminal_failures(app):
    terminal_movie = _create_movie("ABP-008", "MovieA8", desc="")
    retryable_movie = _create_movie("ABP-009", "MovieA9", desc="")
    _create_task_state(
        terminal_movie,
        state=ResourceTaskStateService.STATE_FAILED,
        last_error="DMM 未找到对应番号: ABP-008",
        extra={"terminal": True},
    )
    _create_task_state(
        retryable_movie,
        state=ResourceTaskStateService.STATE_FAILED,
        last_error="DMM 已找到对应番号但详情页没有描述: ABP-009",
        extra={"terminal": False},
    )
    service = MovieDescSyncService(
        catalog_import_service=CatalogImportService(
            dmm_provider=FakeDmmProvider(desc_by_number={"ABP-009": "desc 9"})
        )
    )

    stats = service.run()

    assert stats == {
        "candidate_movies": 1,
        "processed_movies": 1,
        "succeeded_movies": 1,
        "failed_movies": 0,
        "updated_movies": 1,
        "skipped_movies": 0,
    }
    assert Movie.get(Movie.movie_number == "ABP-008").desc == ""
    assert Movie.get(Movie.movie_number == "ABP-009").desc == "desc 9"


def test_movie_desc_sync_service_retries_request_failures(app):
    _create_movie("ABP-010", "MovieA10", desc="")
    _create_task_state(
        Movie.get(Movie.movie_number == "ABP-010"),
        state=ResourceTaskStateService.STATE_FAILED,
        last_error="DMM request failed",
        extra={"terminal": False},
    )
    service = MovieDescSyncService(
        catalog_import_service=CatalogImportService(
            dmm_provider=FakeDmmProvider(failures={"ABP-010": _request_error()})
        )
    )

    stats = service.run()

    assert stats["candidate_movies"] == 1
    assert stats["failed_movies"] == 1


def test_movie_desc_sync_service_does_not_retry_missing_movie_number(app):
    movie = _create_movie("ABP-011", "MovieA11", desc="")
    provider = CountingDmmProvider([DmmMovieNumberNotFoundError(movie.movie_number)])
    service = MovieDescSyncService(
        catalog_import_service=CatalogImportService(dmm_provider=provider)
    )

    first_stats = service.run()
    second_stats = service.run()

    state = ResourceTaskStateService.get_state(MovieDescSyncService.TASK_KEY, movie.id)
    assert first_stats["failed_movies"] == 1
    assert second_stats["candidate_movies"] == 0
    assert provider.calls == [movie.movie_number]
    assert state is not None
    assert state.state == ResourceTaskStateService.STATE_FAILED
    assert state.extra == {"terminal": True}


def test_movie_desc_sync_service_does_not_retry_missing_description(app):
    movie = _create_movie("ABP-012", "MovieA12", desc="")
    provider = CountingDmmProvider([DmmMovieDescNotFoundError(movie.movie_number)])
    service = MovieDescSyncService(
        catalog_import_service=CatalogImportService(dmm_provider=provider)
    )

    first_stats = service.run()
    second_stats = service.run()

    state = ResourceTaskStateService.get_state(MovieDescSyncService.TASK_KEY, movie.id)
    assert first_stats["failed_movies"] == 1
    assert second_stats["candidate_movies"] == 0
    assert provider.calls == [movie.movie_number]
    assert state is not None
    assert state.state == ResourceTaskStateService.STATE_FAILED
    assert state.extra == {"terminal": True}


def test_sync_movie_desc_opens_circuit_after_consecutive_request_errors(app):
    threshold = CatalogImportService.DMM_UNAVAILABLE_FAILURE_THRESHOLD
    movies = [
        _create_movie(f"DMM-10{i}", f"MovieDmm1{i}", desc="")
        for i in range(threshold + 2)
    ]
    # 仅提供 threshold 个连通性失败结果：达阈值熔断后不应再调用 provider。
    provider = CountingDmmProvider([_request_error() for _ in range(threshold)])
    service = CatalogImportService(dmm_provider=provider)

    for movie in movies[:threshold]:
        assert service.sync_movie_desc(movie) is False
    assert service._dmm_circuit_open is True

    # 熔断后剩余影片直接跳过，provider 调用次数仍等于阈值。
    for movie in movies[threshold:]:
        assert service.sync_movie_desc(movie) is False
    assert len(provider.calls) == threshold


def test_sync_movie_desc_not_found_does_not_open_circuit(app):
    movies = [_create_movie(f"DMM-20{i}", f"MovieDmm2{i}", desc="") for i in range(5)]
    provider = CountingDmmProvider(
        [MetadataNotFoundError("movie_desc", movie.movie_number) for movie in movies]
    )
    service = CatalogImportService(dmm_provider=provider)

    for movie in movies:
        assert service.sync_movie_desc(movie) is False
    # 番号不存在属业务性失败，DMM 仍可用，不应熔断，且每部都实际请求。
    assert service._dmm_circuit_open is False
    assert len(provider.calls) == len(movies)


def test_sync_movie_desc_success_resets_failure_counter(app):
    m1 = _create_movie("DMM-301", "MovieDmm31", desc="")
    m2 = _create_movie("DMM-302", "MovieDmm32", desc="")
    m3 = _create_movie("DMM-303", "MovieDmm33", desc="")
    m4 = _create_movie("DMM-304", "MovieDmm34", desc="")
    provider = CountingDmmProvider(
        [_request_error(), _request_error(), "ok desc", _request_error()]
    )
    service = CatalogImportService(dmm_provider=provider)

    service.sync_movie_desc(m1)
    service.sync_movie_desc(m2)
    assert service._dmm_request_failures == 2
    # 成功一次清零连续失败计数，避免跨越非连续失败误判不可用。
    assert service.sync_movie_desc(m3) is True
    assert service._dmm_request_failures == 0
    service.sync_movie_desc(m4)
    assert service._dmm_request_failures == 1
    assert service._dmm_circuit_open is False


def test_recover_interrupted_running_movies_marks_running_as_failed(app):
    running_movie = _create_movie(
        "ABP-005",
        "MovieA5",
        desc="",
    )
    _create_task_state(
        running_movie,
        state="running",
        attempt_count=2,
    )
    pending_movie = _create_movie(
        "ABP-006",
        "MovieA6",
        desc="",
    )
    _create_task_state(
        pending_movie,
        state="pending",
        last_error="pending_error",
    )

    recovered_count = MovieDescSyncService.recover_interrupted_running_movies(
        error_message="影片描述抓取任务中断，等待重试",
    )
    refreshed_running = ResourceTaskState.get(
        ResourceTaskState.task_key == MovieDescSyncService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == running_movie.id,
    )
    refreshed_pending = ResourceTaskState.get(
        ResourceTaskState.task_key == MovieDescSyncService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == pending_movie.id,
    )

    assert recovered_count == 1
    assert refreshed_running.state == "failed"
    assert refreshed_running.last_error == "影片描述抓取任务中断，等待重试"
    assert refreshed_running.attempt_count == 2
    assert refreshed_pending.state == "pending"
    assert refreshed_pending.last_error == "pending_error"


def test_recover_interrupted_running_movies_uses_default_error_message(app):
    running_movie = _create_movie(
        "ABP-007",
        "MovieA7",
        desc="",
    )
    _create_task_state(running_movie, state="running")

    recovered_count = MovieDescSyncService.recover_interrupted_running_movies()
    refreshed_running = ResourceTaskState.get(
        ResourceTaskState.task_key == MovieDescSyncService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == running_movie.id,
    )

    assert recovered_count == 1
    assert refreshed_running.state == "failed"
    assert refreshed_running.last_error == MovieDescSyncService.INTERRUPTED_FETCH_ERROR_MESSAGE
