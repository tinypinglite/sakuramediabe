from src.model import Movie, ResourceTaskState
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.system.resource_task_state_service import ResourceTaskStateService


class RecordingDmmProvider:
    def __init__(self, description: str = "DMM 简介"):
        self.description = description
        self.calls: list[str] = []

    def get_movie_desc(self, movie_number: str) -> str:
        self.calls.append(movie_number)
        return self.description


def _create_movie(movie_number: str) -> Movie:
    return Movie.create(
        movie_number=movie_number,
        javdb_id=f"JavDB-{movie_number}",
        title=movie_number,
        desc="",
    )


def _create_failed_state(movie: Movie, *, terminal: bool, attempt_count: int) -> ResourceTaskState:
    return ResourceTaskState.create(
        task_key=CatalogImportService.TASK_KEY,
        resource_type="movie",
        resource_id=movie.id,
        state=ResourceTaskStateService.STATE_FAILED,
        attempt_count=attempt_count,
        last_error="DMM 未找到对应番号",
        extra={"terminal": terminal},
    )


def test_sync_movie_desc_skips_terminal_failure_from_any_caller(test_db):
    movie = _create_movie("FC2-4874099")
    state = _create_failed_state(movie, terminal=True, attempt_count=246)
    provider = RecordingDmmProvider()

    result = CatalogImportService(dmm_provider=provider).sync_movie_desc(movie)

    refreshed_state = ResourceTaskState.get_by_id(state.id)
    assert result is False
    assert provider.calls == []
    assert refreshed_state.state == ResourceTaskStateService.STATE_FAILED
    assert refreshed_state.attempt_count == 246
    assert refreshed_state.extra == {"terminal": True}


def test_sync_movie_desc_still_retries_non_terminal_failure(test_db):
    movie = _create_movie("MFCS-150")
    state = _create_failed_state(movie, terminal=False, attempt_count=3)
    provider = RecordingDmmProvider(description="可重试后获取的简介")

    result = CatalogImportService(dmm_provider=provider).sync_movie_desc(movie)

    refreshed_movie = Movie.get_by_id(movie.id)
    refreshed_state = ResourceTaskState.get_by_id(state.id)
    assert result is True
    assert provider.calls == [movie.movie_number]
    assert refreshed_movie.desc == "可重试后获取的简介"
    assert refreshed_state.state == ResourceTaskStateService.STATE_SUCCEEDED
    assert refreshed_state.attempt_count == 4
