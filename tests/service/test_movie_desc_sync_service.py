from src.metadata.provider import MetadataNotFoundError
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


def test_movie_desc_sync_service_retries_non_terminal_missing_description_failures(app):
    _create_movie("ABP-010", "MovieA10", desc="")
    _create_task_state(
        Movie.get(Movie.movie_number == "ABP-010"),
        state=ResourceTaskStateService.STATE_FAILED,
        last_error="DMM 已找到对应番号但详情页没有描述: ABP-010",
        extra={"terminal": False},
    )
    service = MovieDescSyncService(
        catalog_import_service=CatalogImportService(
            dmm_provider=FakeDmmProvider(
                failures={"ABP-010": MetadataNotFoundError("movie_desc", "ABP-010")}
            )
        )
    )

    stats = service.run()

    assert stats["candidate_movies"] == 1
    assert stats["failed_movies"] == 1


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
