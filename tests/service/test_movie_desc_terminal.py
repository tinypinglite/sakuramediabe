"""movie_desc_sync 终态语义（Wave 2 kernel 记账版）：
failed_terminal 在公共入口拦截、DMM 确认无此番号落终态、可重试失败正常重试。
"""

from src.metadata._providers.dmm import DmmMovieNumberNotFoundError
from src.model import Movie, ResourceTaskAttempt, ResourceTaskState
from src.service.catalog.catalog_import_service import CatalogImportService


class RecordingDmmProvider:
    def __init__(self, description: str = "DMM 简介"):
        self.description = description
        self.calls: list[str] = []

    def get_movie_desc(self, movie_number: str) -> str:
        self.calls.append(movie_number)
        return self.description


class NotFoundDmmProvider(RecordingDmmProvider):
    def get_movie_desc(self, movie_number: str) -> str:
        self.calls.append(movie_number)
        raise DmmMovieNumberNotFoundError(movie_number)


def _create_movie(movie_number: str) -> Movie:
    return Movie.create(
        movie_number=movie_number,
        javdb_id=f"JavDB-{movie_number}",
        title=movie_number,
        desc="",
    )


def _create_state(movie: Movie, *, state: str, attempt_count: int) -> ResourceTaskState:
    return ResourceTaskState.create(
        task_key=CatalogImportService.TASK_KEY,
        resource_type="movie",
        resource_id=movie.id,
        state=state,
        attempt_count=attempt_count,
        last_error="DMM 未找到对应番号",
        error_code="dmm_movie_number_not_found",
    )


def test_sync_movie_desc_skips_terminal_failure_from_any_caller(test_db):
    movie = _create_movie("FC2-4874099")
    state = _create_state(movie, state="failed_terminal", attempt_count=2)
    provider = RecordingDmmProvider()

    result = CatalogImportService(dmm_provider=provider).sync_movie_desc(movie)

    refreshed_state = ResourceTaskState.get_by_id(state.id)
    assert result is False
    assert provider.calls == []
    assert refreshed_state.state == "failed_terminal"
    assert refreshed_state.attempt_count == 2


def test_sync_movie_desc_marks_terminal_with_error_code_on_dmm_not_found(test_db):
    movie = _create_movie("ABC-404")
    provider = NotFoundDmmProvider()

    result = CatalogImportService(dmm_provider=provider).sync_movie_desc(movie)

    record = ResourceTaskState.get(ResourceTaskState.resource_id == movie.id)
    assert result is False
    assert record.state == "failed_terminal"
    assert record.error_code == "dmm_movie_number_not_found"
    assert record.next_retry_at is None
    attempt = ResourceTaskAttempt.get(ResourceTaskAttempt.resource_id == movie.id)
    assert attempt.state == "failed"
    assert attempt.retryable is False
    # 终态被入口拦截：再次调用不再请求 DMM。
    assert CatalogImportService(dmm_provider=provider).sync_movie_desc(movie) is False
    assert provider.calls == [movie.movie_number]


def test_sync_movie_desc_still_retries_non_terminal_failure(test_db):
    movie = _create_movie("MFCS-150")
    state = _create_state(movie, state="failed_retryable", attempt_count=3)
    provider = RecordingDmmProvider(description="可重试后获取的简介")

    result = CatalogImportService(dmm_provider=provider).sync_movie_desc(movie)

    refreshed_movie = Movie.get_by_id(movie.id)
    refreshed_state = ResourceTaskState.get_by_id(state.id)
    assert result is True
    assert provider.calls == [movie.movie_number]
    assert refreshed_movie.desc == "可重试后获取的简介"
    assert refreshed_state.state == "succeeded"
    assert refreshed_state.attempt_count == 4
    assert refreshed_state.error_code is None
