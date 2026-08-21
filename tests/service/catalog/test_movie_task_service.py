from src.model import BackgroundTaskRun, Movie
from src.service.catalog.movie_task_service import MovieTaskService


def test_recompute_movie_heat_enqueues_a_parameterized_task(test_db):
    movie = Movie.create(
        javdb_id="javdb-heat-task",
        movie_number="ABP-001",
        title="热度任务",
    )

    response = MovieTaskService.recompute_movie_heat(" abp-001 ")

    task_run = BackgroundTaskRun.get_by_id(response.task_run_id)
    assert movie.movie_number == "ABP-001"
    assert response.task_key == "movie_heat_update"
    assert response.state == "pending"
    assert task_run.params == {"movie_number": "ABP-001"}
    assert task_run.scheduled_at is not None
