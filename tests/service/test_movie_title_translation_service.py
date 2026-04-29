from datetime import datetime

import pytest

from src.config.config import settings
from src.model import Actor, Movie, MovieActor, ResourceTaskState
from src.service.catalog.movie_desc_translation_client import MovieDescTranslationClientError
from src.service.catalog.movie_title_translation_service import (
    MovieTitleTranslationService,
    MovieTitleTranslationTaskAbortError,
)


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


def _get_task_state(movie_id: int) -> ResourceTaskState:
    return ResourceTaskState.get(
        ResourceTaskState.task_key == MovieTitleTranslationService.TASK_KEY,
        ResourceTaskState.resource_type == "movie",
        ResourceTaskState.resource_id == movie_id,
    )


def test_movie_title_translation_service_only_processes_pending_movies(app, tmp_path, monkeypatch):
    _create_movie("ABP-101", "Movie101", title="原始标题")
    _create_movie("ABP-102", "Movie102", title="", title_zh="")
    _create_movie("ABP-103", "Movie103", title="已翻译标题", title_zh="已经翻译")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            assert "中文标题" in system_prompt
            assert source_text == "原始标题"
            return "中文标题"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("请输出中文标题", encoding="utf-8")
    service = MovieTitleTranslationService(
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
    assert refreshed.title_zh == "中文标题"
    assert task_state.state == MovieTitleTranslationService.TRANSLATION_STATUS_SUCCEEDED


def test_movie_title_translation_service_prioritizes_subscribed_then_subscribed_actor_then_heat(
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
        title="最早订阅影片",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 1, 9, 0, 0),
        heat=1,
    )
    _create_movie(
        "ABP-111",
        "Movie111",
        title="较晚订阅影片",
        is_subscribed=True,
        subscribed_at=datetime(2026, 3, 5, 9, 0, 0),
        heat=99,
    )
    high_heat_subscribed_actor_movie = _create_movie(
        "ABP-112",
        "Movie112",
        title="高热度订阅女优影片",
        heat=80,
    )
    _create_movie(
        "ABP-113",
        "Movie113",
        title="低热度订阅女优影片",
        heat=20,
    )
    _create_movie(
        "ABP-114",
        "Movie114",
        title="高热度普通影片",
        heat=70,
    )
    _create_movie(
        "ABP-115",
        "Movie115",
        title="低热度普通影片",
        heat=10,
    )
    unrelated_actor_movie = _create_movie(
        "ABP-116",
        "Movie116",
        title="未订阅女优影片",
        heat=90,
    )

    MovieActor.create(movie=high_heat_subscribed_actor_movie, actor=subscribed_actor)
    MovieActor.create(movie=Movie.get(Movie.movie_number == "ABP-113"), actor=subscribed_actor)
    MovieActor.create(movie=unrelated_actor_movie, actor=unsubscribed_actor)

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            translation_order.append(source_text)
            return f"{source_text}-中文"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("请输出中文标题", encoding="utf-8")
    service = MovieTitleTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()

    assert stats["candidate_movies"] == 7
    assert translation_order == [
        "最早订阅影片",
        "较晚订阅影片",
        "高热度订阅女优影片",
        "低热度订阅女优影片",
        "未订阅女优影片",
        "高热度普通影片",
        "低热度普通影片",
    ]
    assert Movie.get_by_id(early_subscribed_movie.id).title_zh == "最早订阅影片-中文"


def test_movie_title_translation_service_marks_failure_when_client_errors(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-201", "Movie201", title="翻译标题")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            raise RuntimeError("upstream_failed")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("请输出中文标题", encoding="utf-8")
    service = MovieTitleTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()
    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)

    assert stats["failed_movies"] == 1
    assert refreshed.title_zh == ""
    assert task_state.state == MovieTitleTranslationService.TRANSLATION_STATUS_FAILED
    assert task_state.last_error == "upstream_failed"


def test_movie_title_translation_service_returns_disabled_stats_when_disabled(app, monkeypatch):
    _create_movie("ABP-301", "Movie301", title="未启用测试")
    monkeypatch.setattr(settings.movie_desc_translation, "enabled", False, raising=False)

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:  # pragma: no cover
            raise AssertionError("should not call translate")

    service = MovieTitleTranslationService(translation_client=FakeTranslationClient())

    assert service.run() == {
        "candidate_movies": 0,
        "processed_movies": 0,
        "succeeded_movies": 0,
        "failed_movies": 0,
        "updated_movies": 0,
        "skipped_movies": 0,
    }


def test_movie_title_translation_service_skips_movie_updated_concurrently(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-501", "Movie501", title="并发更新标题")

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:  # pragma: no cover
            raise AssertionError("should not call translate")

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("请输出中文标题", encoding="utf-8")
    service = MovieTitleTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )
    original_get_by_id = Movie.get_by_id
    call_state = {"count": 0}

    def fake_get_by_id(movie_id: int):
        if call_state["count"] == 0:
            Movie.update(title_zh="已有中文标题").where(Movie.id == movie_id).execute()
        call_state["count"] += 1
        return original_get_by_id(movie_id)

    monkeypatch.setattr("src.service.catalog.movie_title_translation_service.Movie.get_by_id", fake_get_by_id)

    stats = service.run()

    assert stats["candidate_movies"] == 1
    assert stats["processed_movies"] == 1
    assert stats["skipped_movies"] == 1


def test_movie_title_translation_service_retries_429_then_succeeds(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-211", "Movie211", title="再试成功")
    sleep_calls = []
    call_state = {"count": 0}

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            call_state["count"] += 1
            if call_state["count"] == 1:
                raise MovieDescTranslationClientError(429, "rate_limit", "too many requests")
            return "重试后的中文标题"

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    monkeypatch.setattr("src.service.catalog.movie_title_translation_service.time.sleep", lambda seconds: sleep_calls.append(seconds))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("请输出中文标题", encoding="utf-8")
    service = MovieTitleTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    stats = service.run()

    assert stats["succeeded_movies"] == 1
    assert sleep_calls == [5]
    assert Movie.get_by_id(movie.id).title_zh == "重试后的中文标题"


def test_movie_title_translation_service_aborts_task_for_retryable_errors(app, tmp_path, monkeypatch):
    movie = _create_movie("ABP-212", "Movie212", title="持续失败")
    sleep_calls = []

    class FakeTranslationClient:
        def translate(self, *, system_prompt: str, source_text: str) -> str:
            raise MovieDescTranslationClientError(
                503,
                "movie_desc_translation_unavailable",
                "service unavailable",
            )

    monkeypatch.setattr(settings.movie_desc_translation, "enabled", True, raising=False)
    monkeypatch.setattr("src.service.catalog.movie_title_translation_service.time.sleep", lambda seconds: sleep_calls.append(seconds))
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("请输出中文标题", encoding="utf-8")
    service = MovieTitleTranslationService(
        translation_client=FakeTranslationClient(),
        prompt_path=prompt_path,
    )

    with pytest.raises(MovieTitleTranslationTaskAbortError) as exc_info:
        service.run()

    refreshed = Movie.get_by_id(movie.id)
    task_state = _get_task_state(movie.id)
    assert "影片标题翻译任务中断" in exc_info.value.message
    assert refreshed.title_zh == ""
    assert task_state.state == MovieTitleTranslationService.TRANSLATION_STATUS_PENDING
    assert sleep_calls == [5, 10, 20]
