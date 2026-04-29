from datetime import datetime
from pathlib import Path

import pytest

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import Actor, BackgroundTaskRun, Movie, MovieActor, ResourceTaskState
from src.service.catalog.movie_desc_translation_client import MovieDescTranslationClientError
from src.service.catalog.movie_desc_translation_service import (
    MovieDescTranslationService,
    MovieDescTranslationTaskAbortError,
)
from src.service.system import ActivityService


def _create_movie(movie_number: str, javdb_id: str, **kwargs):
    payload = {
        "movie_number": movie_number,
        "javdb_id": javdb_id,
        "title": kwargs.pop("title", movie_number),
    }
    payload.update(kwargs)
    return Movie.create(**payload)


def _create_actor(name: str, javdb_id: str, **kwargs):
    payload = {
        "name": name,
        "javdb_id": javdb_id,
    }
    payload.update(kwargs)
    return Actor.create(**payload)


def _create_task_state(movie: Movie, **kwargs) -> ResourceTaskState:
    payload = {
        "task_key": MovieDescTranslationService.TASK_KEY,
        "resource_type": "movie",
        "resource_id": movie.id,
    }
    payload.update(kwargs)
    return ResourceTaskState.create(**payload)


def _get_task_state(movie_id: int) -> ResourceTaskState:
    return ResourceTaskState.get(
        ResourceTaskState.task_key == MovieDescTranslationService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie_id,
    )


def test_movie_desc_translation_service_only_processes_pending_movies(app, tmp_path, monkeypatch):
    _create_movie("ABP-101", "Movie101", desc="これは最初の説明です")
    _create_movie("ABP-102", "Movie102", desc="", desc_zh="")
    _create_movie("ABP-103", "Movie103", desc="すでに翻訳済み", desc_zh="已经翻译")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            assert "翻译助手" in system_prompt
            assert source_text == "これは最初の説明です"
            return "这是第一段简介"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
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
    refreshed = Movie.get(Movie.movie_number == "ABP-101")
    task_state = _get_task_state(refreshed.id)
    assert refreshed.desc == "これは最初の説明です"
    assert refreshed.desc_zh == "这是第一段简介"
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_SUCCEEDED


def test_movie_desc_translation_service_prioritizes_subscribed_then_subscribed_actor_then_heat(
    app,
    tmp_path,
    monkeypatch,
):
    subscribed_actor = _create_actor("三上悠亚", "ActorA1", is_subscribed=True)
    unsubscribed_actor = _create_actor("鬼头桃菜", "ActorA2", is_subscribed=False)
    translation_order = []

    early_subscribed_movie = _create_movie(
        "ABP-110",
        "Movie110",
        desc="最早订阅影片",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 1, 9, 0, 0),
        heat=1,
    )
    late_subscribed_movie = _create_movie(
        "ABP-111",
        "Movie111",
        desc="较晚订阅影片",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 5, 9, 0, 0),
        heat=99,
    )
    high_heat_subscribed_actor_movie = _create_movie(
        "ABP-112",
        "Movie112",
        desc="高热度订阅女优影片",
        heat=80,
    )
    low_heat_subscribed_actor_movie = _create_movie(
        "ABP-113",
        "Movie113",
        desc="低热度订阅女优影片",
        heat=20,
    )
    high_heat_regular_movie = _create_movie(
        "ABP-114",
        "Movie114",
        desc="高热度普通影片",
        heat=70,
    )
    low_heat_regular_movie = _create_movie(
        "ABP-115",
        "Movie115",
        desc="低热度普通影片",
        heat=10,
    )
    unrelated_actor_movie = _create_movie(
        "ABP-116",
        "Movie116",
        desc="未订阅女优影片",
        heat=90,
    )

    MovieActor.create(movie=high_heat_subscribed_actor_movie, actor=subscribed_actor)
    MovieActor.create(movie=low_heat_subscribed_actor_movie, actor=subscribed_actor)
    MovieActor.create(movie=unrelated_actor_movie, actor=unsubscribed_actor)

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            translation_order.append(source_text)
            return f"{source_text}-中文"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()

    assert stats == {
        "candidate_movies": 7,
        "processed_movies": 7,
        "succeeded_movies": 7,
        "failed_movies": 0,
        "updated_movies": 7,
        "skipped_movies": 0,
    }
    assert translation_order == [
        "最早订阅影片",
        "较晚订阅影片",
        "高热度订阅女优影片",
        "低热度订阅女优影片",
        "未订阅女优影片",
        "高热度普通影片",
        "低热度普通影片",
    ]
    assert Movie.get_by_id(early_subscribed_movie.id).desc_zh == "最早订阅影片-中文"
    assert Movie.get_by_id(low_heat_regular_movie.id).desc_zh == "低热度普通影片-中文"


def test_movie_desc_translation_service_only_translates_overlapping_movie_once_in_subscribed_bucket(
    app,
    tmp_path,
    monkeypatch,
):
    subscribed_actor = _create_actor("河北彩花", "ActorB1", is_subscribed=True)
    translation_order = []

    overlapping_movie = _create_movie(
        "ABP-120",
        "Movie120",
        desc="已订阅且关联订阅女优",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 2, 9, 0, 0),
        heat=5,
    )
    subscribed_actor_only_movie = _create_movie(
        "ABP-121",
        "Movie121",
        desc="仅关联订阅女优",
        heat=100,
    )

    MovieActor.create(movie=overlapping_movie, actor=subscribed_actor)
    MovieActor.create(movie=subscribed_actor_only_movie, actor=subscribed_actor)

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            translation_order.append(source_text)
            return f"{source_text}-中文"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()

    assert stats == {
        "candidate_movies": 2,
        "processed_movies": 2,
        "succeeded_movies": 2,
        "failed_movies": 0,
        "updated_movies": 2,
        "skipped_movies": 0,
    }
    assert translation_order == [
        "已订阅且关联订阅女优",
        "仅关联订阅女优",
    ]


def test_movie_desc_translation_service_marks_failure_when_client_errors(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-201", "Movie201", desc="翻訳対象")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            raise RuntimeError("upstream_failed")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()
    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)
    task_state = _get_task_state(movie.id)

    assert stats["failed_movies"] == 1
    assert refreshed.desc == "翻訳対象"
    assert refreshed.desc_zh == ""
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_FAILED
    assert task_state.last_error == "upstream_failed"


def test_movie_desc_translation_service_treats_no_valid_content_as_success(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-202", "Movie202", desc="广告内容测试")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            assert source_text == "广告内容测试"
            return "  无有效内容  "

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()
    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)

    assert stats == {
        "candidate_movies": 1,
        "processed_movies": 1,
        "succeeded_movies": 1,
        "failed_movies": 0,
        "updated_movies": 1,
        "skipped_movies": 0,
    }
    assert refreshed.desc_zh == ""
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_SUCCEEDED
    assert task_state.last_error is None


def test_movie_desc_translation_service_returns_disabled_stats_when_disabled(app, monkeypatch):
    _create_movie("ABP-301", "Movie301", desc="未启用测试")
    monkeypatch.setattr(settings.movie_desc_translation, "enabled", False, raising=False)

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:  # pragma: no cover
            raise AssertionError("should not call translate")

    service = MovieDescTranslationService(translation_client=FakeTranslationClient())

    assert service.run() == {
        "candidate_movies": 0,
        "processed_movies": 0,
        "succeeded_movies": 0,
        "failed_movies": 0,
        "updated_movies": 0,
        "skipped_movies": 0,
    }


def test_movie_desc_translation_service_marks_failures_when_prompt_missing(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-401", "Movie401", desc="プロンプト欠失")
    missing_prompt_path = tmp_path / "missing.md"

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:  # pragma: no cover
            raise AssertionError("should not call translate")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=missing_prompt_path,
    )

    with pytest.raises(MovieDescTranslationTaskAbortError) as exc_info:
        service.run()
    task_state = ResourceTaskState.get_or_none(
        ResourceTaskState.task_key == MovieDescTranslationService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie.id,
    )

    assert "movie_desc_translation_prompt_missing" in exc_info.value.message
    assert task_state is None


def test_movie_desc_translation_service_skips_movie_updated_concurrently(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-501", "Movie501", desc="同時更新テスト")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:  # pragma: no cover
            raise AssertionError("should not call translate")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )
    original_get_by_id = Movie.get_by_id
    call_state = {"count": 0}

    def fake_get_by_id(movie_id: int):
        if call_state["count"] == 0:
            Movie.update(desc_zh="已有中文").where(Movie.id == movie_id).execute()
        call_state["count"] += 1
        return original_get_by_id(movie_id)

    monkeypatch.setattr("src.service.catalog.movie_desc_translation_service.Movie.get_by_id", fake_get_by_id)

    stats = service.run()

    assert stats["candidate_movies"] == 1
    assert stats["processed_movies"] == 1
    assert stats["skipped_movies"] == 1


def test_movie_desc_translation_service_does_not_retry_succeeded_empty_translation(
    app,
    tmp_path,
    monkeypatch,
):
    movie = _create_movie(
        "ABP-502",
        "Movie502",
        desc="已处理为空结果",
        desc_zh="",
    )
    _create_task_state(movie, state=MovieDescTranslationService.TRANSLATION_STATUS_SUCCEEDED)

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:  # pragma: no cover
            raise AssertionError("should not call translate")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()

    assert stats == {
        "candidate_movies": 0,
        "processed_movies": 0,
        "succeeded_movies": 0,
        "failed_movies": 0,
        "updated_movies": 0,
        "skipped_movies": 0,
    }


def test_movie_desc_translation_service_retries_429_then_succeeds(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-211", "Movie211", desc="再试成功")
    sleep_calls = []
    call_state = {"count": 0}

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            call_state["count"] += 1
            if call_state["count"] <= 3:
                raise MovieDescTranslationClientError(429, "rate_limit", "too many requests")
            return "重试后的中文简介"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    monkeypatch.setattr("src.service.catalog.movie_desc_translation_service.time.sleep", lambda seconds: sleep_calls.append(seconds))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()
    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)

    assert call_state["count"] == 4
    assert sleep_calls == [5, 10, 20]
    assert stats["succeeded_movies"] == 1
    assert stats["failed_movies"] == 0
    assert refreshed.desc_zh == "重试后的中文简介"
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_SUCCEEDED
    assert task_state.attempt_count == 1


@pytest.mark.parametrize(
    ("status_code", "error_code", "message"),
    [
        (429, "rate_limit", "too many requests"),
        (503, "movie_desc_translation_failed", "service unavailable"),
        (503, "movie_desc_translation_unavailable", "network unavailable"),
        (200, "movie_desc_translation_invalid_response", "invalid payload"),
    ],
)
def test_movie_desc_translation_service_aborts_task_for_retryable_errors(
    app,
    tmp_path,
    monkeypatch,
    status_code: int,
    error_code: str,
    message: str,
):
    movie = _create_movie("ABP-701", "Movie701", desc="需要中断任务")
    sleep_calls = []
    call_state = {"count": 0}

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            call_state["count"] += 1
            raise MovieDescTranslationClientError(status_code, error_code, message)

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    monkeypatch.setattr("src.service.catalog.movie_desc_translation_service.time.sleep", lambda seconds: sleep_calls.append(seconds))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    with pytest.raises(MovieDescTranslationTaskAbortError) as exc_info:
        service.run()

    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)
    assert call_state["count"] == 4
    assert sleep_calls == [5, 10, 20]
    assert "movie_number=ABP-701" in exc_info.value.message
    assert message in exc_info.value.message
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_PENDING
    assert task_state.last_error == exc_info.value.message
    assert task_state.attempt_count == 1
    assert refreshed.desc_zh == ""


def test_movie_desc_translation_service_marks_permanent_client_error_as_failed_and_continues(
    app,
    tmp_path,
    monkeypatch,
):
    first_movie = _create_movie("ABP-811", "Movie811", desc="无效请求", heat=10)
    second_movie = _create_movie("ABP-812", "Movie812", desc="后续影片", heat=0)
    call_state = {"count": 0}
    sleep_calls = []

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            call_state["count"] += 1
            if call_state["count"] == 1:
                raise MovieDescTranslationClientError(400, "bad_request", "invalid request")
            return "后续影片译文"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    monkeypatch.setattr("src.service.catalog.movie_desc_translation_service.time.sleep", lambda seconds: sleep_calls.append(seconds))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()
    refreshed_first = Movie.get_by_id(first_movie.id)
    refreshed_second = Movie.get_by_id(second_movie.id)
    first_task_state = _get_task_state(first_movie.id)
    second_task_state = _get_task_state(second_movie.id)

    assert sleep_calls == []
    assert stats == {
        "candidate_movies": 2,
        "processed_movies": 2,
        "succeeded_movies": 1,
        "failed_movies": 1,
        "updated_movies": 1,
        "skipped_movies": 0,
    }
    assert first_task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_FAILED
    assert first_task_state.last_error == "invalid request"
    assert refreshed_first.desc_zh == ""
    assert second_task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_SUCCEEDED
    assert refreshed_second.desc_zh == "后续影片译文"


def test_movie_desc_translation_service_marks_empty_result_as_failed(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-801", "Movie801", desc="空译文测试")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            raise MovieDescTranslationClientError(
                200,
                "movie_desc_translation_empty_result",
                "影片简介翻译服务返回了空译文",
            )

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()
    task_state = _get_task_state(movie.id)

    assert stats["failed_movies"] == 1
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_FAILED
    assert task_state.last_error == "影片简介翻译服务返回了空译文"


def test_translate_movie_overrides_existing_desc_zh_and_marks_manual_state(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-951", "Movie951", desc="原始简介", desc_zh="旧译文")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            assert source_text == "原始简介"
            return "新译文"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    result = ActivityService.run_task(
        task_key="movie_desc_translation",
        trigger_type="manual",
        func=lambda _reporter: service.translate_movie(movie),
    )

    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)

    assert result == {
        "movie_id": movie.id,
        "movie_number": "ABP-951",
        "updated_movies": 1,
    }
    assert refreshed.desc_zh == "新译文"
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_SUCCEEDED
    assert task_state.last_trigger_type == "manual"


def test_translate_movie_rejects_movie_without_desc(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-952", "Movie952", desc="")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:  # pragma: no cover
            raise AssertionError("should not call translate")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    with pytest.raises(ApiError) as exc_info:
        service.translate_movie(movie)

    task_state = ResourceTaskState.get_or_none(
        ResourceTaskState.task_key == MovieDescTranslationService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie.id,
    )

    assert exc_info.value.code == "movie_desc_missing"
    assert task_state is None


def test_translate_movie_marks_retryable_error_as_failed_for_single_movie(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-953", "Movie953", desc="重试失败")
    sleep_calls = []

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            raise MovieDescTranslationClientError(429, "rate_limit", "too many requests")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    monkeypatch.setattr("src.service.catalog.movie_desc_translation_service.time.sleep", lambda seconds: sleep_calls.append(seconds))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    with pytest.raises(MovieDescTranslationTaskAbortError):
        ActivityService.run_task(
            task_key="movie_desc_translation",
            trigger_type="manual",
            func=lambda _reporter: service.translate_movie(movie),
        )

    task_run = BackgroundTaskRun.get()
    task_state = _get_task_state(movie.id)

    assert sleep_calls == [5, 10, 20]
    assert task_run.state == "failed"
    assert task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_FAILED
    assert task_state.last_trigger_type == "manual"
    assert task_state.attempt_count == 1
    assert "movie_number=ABP-953" in (task_state.last_error or "")


def test_activity_service_marks_translation_task_failed_with_partial_summary(app, tmp_path, monkeypatch):
    first_movie = _create_movie("ABP-901", "Movie901", desc="第一部", heat=10)
    second_movie = _create_movie("ABP-902", "Movie902", desc="第二部", heat=0)
    sleep_calls = []
    call_state = {"count": 0}

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            call_state["count"] += 1
            if call_state["count"] == 1:
                return "第一部中文简介"
            raise MovieDescTranslationClientError(429, "rate_limit", "too many requests")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    monkeypatch.setattr("src.service.catalog.movie_desc_translation_service.time.sleep", lambda seconds: sleep_calls.append(seconds))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("你是一个翻译助手", encoding="utf-8")
    service = MovieDescTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    with pytest.raises(MovieDescTranslationTaskAbortError):
        ActivityService.run_task(
            task_key="movie_desc_translation",
            trigger_type="manual",
            func=lambda reporter: service.run(progress_callback=reporter.progress_callback),
        )

    task_run = BackgroundTaskRun.get()
    refreshed_first = Movie.get_by_id(first_movie.id)
    refreshed_second = Movie.get_by_id(second_movie.id)
    first_task_state = _get_task_state(first_movie.id)
    second_task_state = _get_task_state(second_movie.id)

    assert call_state["count"] == 5
    assert sleep_calls == [5, 10, 20]
    assert task_run.state == "failed"
    assert "movie_number=ABP-902" in (task_run.error_message or "")
    assert task_run.result_summary == {
        "candidate_movies": 2,
        "processed_movies": 2,
        "succeeded_movies": 1,
        "failed_movies": 0,
        "updated_movies": 1,
        "skipped_movies": 0,
    }
    assert first_task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_SUCCEEDED
    assert second_task_state.state == MovieDescTranslationService.TRANSLATION_STATUS_PENDING
    assert second_task_state.attempt_count == 1


def test_recover_interrupted_running_movies_marks_running_as_failed(app):
    running_movie = _create_movie(
        "ABP-601",
        "Movie601",
        desc="中断翻译",
    )
    _create_task_state(
        running_movie,
        state=MovieDescTranslationService.TRANSLATION_STATUS_RUNNING,
        attempt_count=2,
    )
    pending_movie = _create_movie(
        "ABP-602",
        "Movie602",
        desc="待处理",
    )
    _create_task_state(
        pending_movie,
        state=MovieDescTranslationService.TRANSLATION_STATUS_PENDING,
        last_error="pending_error",
    )

    recovered_count = MovieDescTranslationService.recover_interrupted_running_movies(
        error_message="影片简介翻译任务中断，等待重试",
    )
    refreshed_running = _get_task_state(running_movie.id)
    refreshed_pending = _get_task_state(pending_movie.id)

    assert recovered_count == 1
    assert refreshed_running.state == MovieDescTranslationService.TRANSLATION_STATUS_FAILED
    assert refreshed_running.last_error == "影片简介翻译任务中断，等待重试"
    assert refreshed_running.attempt_count == 2
    assert refreshed_pending.state == MovieDescTranslationService.TRANSLATION_STATUS_PENDING
    assert refreshed_pending.last_error == "pending_error"


def test_recover_interrupted_running_movies_uses_default_error_message(app):
    running_movie = _create_movie(
        "ABP-603",
        "Movie603",
        desc="默认错误信息",
    )
    _create_task_state(
        running_movie,
        state=MovieDescTranslationService.TRANSLATION_STATUS_RUNNING,
    )

    recovered_count = MovieDescTranslationService.recover_interrupted_running_movies()
    refreshed_running = _get_task_state(running_movie.id)

    assert recovered_count == 1
    assert refreshed_running.state == MovieDescTranslationService.TRANSLATION_STATUS_FAILED
    assert refreshed_running.last_error == MovieDescTranslationService.INTERRUPTED_TRANSLATION_ERROR_MESSAGE
