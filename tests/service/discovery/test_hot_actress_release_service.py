from datetime import date, datetime, time, timedelta
from math import log1p

import pytest

from src.model import Actor, Movie, MovieActor
from src.service.discovery.hot_actress_release_service import HotActressReleaseService

TODAY = date(2026, 8, 23)


def _create_actor(name: str, gender: int = 1) -> Actor:
    return Actor.create(javdb_id=f"actor-{name}", name=name, gender=gender)


def _create_movie(
    movie_number: str,
    release_date: date,
    heat: int,
    actors: list[Actor],
    *,
    is_collection: bool = False,
) -> Movie:
    movie = Movie.create(
        javdb_id=f"movie-{movie_number}",
        movie_number=movie_number,
        title=movie_number,
        release_date=datetime.combine(release_date, time.min),
        heat=heat,
        is_collection=is_collection,
    )
    for actor in actors:
        MovieActor.create(movie=movie, actor=actor)
    return movie


def test_hot_actress_releases_use_only_matured_single_female_history_and_keep_duplicates(
    test_db,
    monkeypatch,
):
    monkeypatch.setattr(HotActressReleaseService, "_today", staticmethod(lambda: TODAY))
    actress = _create_actor("lead")
    second_actress = _create_actor("second")
    male_actor = _create_actor("male", gender=2)

    historical_dates = [TODAY - timedelta(days=170), TODAY - timedelta(days=140), TODAY - timedelta(days=100)]
    historical_heats = [900, 600, 300]
    for index, (release_date, heat) in enumerate(zip(historical_dates, historical_heats), start=1):
        _create_movie(f"HISTORY-{index}", release_date, heat, [actress])

    # 多女优、尚未成熟和合集都不能作为女优历史表现的证据。
    _create_movie("MULTI-FEMALE", TODAY - timedelta(days=90), 100_000, [actress, second_actress])
    _create_movie("TOO-NEW", TODAY - timedelta(days=30), 100_000, [actress])
    _create_movie("COLLECTION", TODAY - timedelta(days=90), 100_000, [actress], is_collection=True)

    first_new = _create_movie("NEW-1", TODAY + timedelta(days=10), 0, [actress])
    second_new = _create_movie("NEW-2", TODAY + timedelta(days=20), 0, [actress, second_actress])
    self_history = _create_movie("SELF-HISTORY", TODAY - timedelta(days=80), 100_000, [actress])
    _create_movie("MALE-ONLY", TODAY + timedelta(days=15), 0, [male_actor])
    _create_movie("COLLECTION-CANDIDATE", TODAY + timedelta(days=15), 0, [actress], is_collection=True)

    actor_evidence = HotActressReleaseService._history_actor_evidence(TODAY)
    assert actor_evidence[actress.id].movie_count == 4

    response = HotActressReleaseService.list_items(page=1, page_size=20)
    items_by_number = {item.movie_number: item for item in response.items}
    assert set(items_by_number) == {
        "MULTI-FEMALE",
        "NEW-1",
        "NEW-2",
        "SELF-HISTORY",
        "TOO-NEW",
    }
    assert response.total == 5
    assert items_by_number["NEW-2"].hot_actress.id == actress.id

    expected_self_score = sum(
        log1p(heat / (TODAY - release_date).days)
        for heat, release_date in zip(historical_heats, historical_dates)
    ) / len(historical_heats)
    assert items_by_number["SELF-HISTORY"].recommendation_score == pytest.approx(
        round(expected_self_score, 4)
    )
    assert items_by_number["SELF-HISTORY"].hot_actress.historical_movie_count == 3
    assert first_new.id != second_new.id != self_history.id


def test_hot_actress_releases_paginate_scored_movies(test_db, monkeypatch):
    monkeypatch.setattr(HotActressReleaseService, "_today", staticmethod(lambda: TODAY))
    actress = _create_actor("lead")
    for index, age_days in enumerate((170, 140, 100), start=1):
        _create_movie(f"HISTORY-{index}", TODAY - timedelta(days=age_days), 900, [actress])
    for index in range(3):
        _create_movie(f"NEW-{index}", TODAY + timedelta(days=index), 0, [actress])

    first_page = HotActressReleaseService.list_items(page=1, page_size=2)
    second_page = HotActressReleaseService.list_items(page=2, page_size=2)

    assert first_page.total == 3
    assert [item.movie_number for item in first_page.items] == ["NEW-2", "NEW-1"]
    assert [item.movie_number for item in second_page.items] == ["NEW-0"]
