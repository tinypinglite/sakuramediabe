from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import log1p

from peewee import JOIN, Case, fn

from src.common.runtime_time import runtime_now
from src.common.service_helpers import validate_page, with_movie_card_relations
from src.model import Actor, Image, Movie, MovieActor
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.pagination import PageResponse
from src.schema.discovery import HotActressReleaseMovieResource, HotActressResource
from src.service.discovery.recommendation_service import MovieRecommendationService


@dataclass(frozen=True)
class _ActorEvidence:
    total: float
    movie_count: int
    by_movie_id: dict[int, float]


@dataclass(frozen=True)
class _ScoredMovie:
    movie_id: int
    release_date: datetime
    actor_id: int
    historical_movie_count: int
    score: float


class HotActressReleaseService:
    """按女性演员已成熟作品表现发现新片，不改变影片自身热度语义。"""

    FEMALE_GENDER = 1
    CANDIDATE_PAST_DAYS = 90
    CANDIDATE_FUTURE_DAYS = 90
    HISTORY_LOOKBACK_DAYS = 180
    HISTORY_MATURITY_DAYS = 60
    MIN_HISTORICAL_MOVIES = 3

    @staticmethod
    def _today() -> date:
        return runtime_now().date()

    @classmethod
    def _history_actor_evidence(cls, today: date) -> dict[int, _ActorEvidence]:
        history_start = today - timedelta(days=cls.HISTORY_LOOKBACK_DAYS)
        history_end = today - timedelta(days=cls.HISTORY_MATURITY_DAYS)
        single_female_history_ids = (
            MovieActor.select(MovieActor.movie)
            .join(Movie)
            .switch(MovieActor)
            .join(Actor)
            .where(
                Movie.is_collection == False,
                Movie.is_blacklisted == False,
                Movie.release_date >= history_start,
                Movie.release_date < history_end,
            )
            .group_by(MovieActor.movie)
            .having(fn.SUM(Case(None, [(Actor.gender == cls.FEMALE_GENDER, 1)], 0)) == 1)
        )
        history_rows = (
            MovieActor.select(MovieActor.movie, MovieActor.actor, Movie.heat, Movie.release_date)
            .join(Movie)
            .switch(MovieActor)
            .join(Actor)
            .where(
                MovieActor.movie.in_(single_female_history_ids),
                Actor.gender == cls.FEMALE_GENDER,
            )
            .tuples()
        )

        totals: dict[int, float] = defaultdict(float)
        counts: dict[int, int] = defaultdict(int)
        evidence_by_actor: dict[int, dict[int, float]] = defaultdict(dict)
        for movie_id, actor_id, heat, release_date in history_rows:
            released_on = cls._release_date(release_date)
            age_days = max((today - released_on).days, cls.HISTORY_MATURITY_DAYS)
            evidence = log1p(float(heat or 0) / age_days)
            normalized_actor_id = int(actor_id)
            totals[normalized_actor_id] += evidence
            counts[normalized_actor_id] += 1
            evidence_by_actor[normalized_actor_id][int(movie_id)] = evidence

        return {
            actor_id: _ActorEvidence(
                total=totals[actor_id],
                movie_count=counts[actor_id],
                by_movie_id=evidence_by_actor[actor_id],
            )
            for actor_id in counts
        }

    @classmethod
    def _candidate_rows(cls, today: date):
        candidate_start = today - timedelta(days=cls.CANDIDATE_PAST_DAYS)
        candidate_end = today + timedelta(days=cls.CANDIDATE_FUTURE_DAYS)
        return (
            Movie.select(Movie.id, Movie.release_date, MovieActor.actor)
            .join(MovieActor)
            .join(Actor)
            .where(
                Movie.is_collection == False,
                Movie.is_blacklisted == False,
                Movie.release_date >= candidate_start,
                Movie.release_date < candidate_end,
                Actor.gender == cls.FEMALE_GENDER,
            )
            .tuples()
        )

    @staticmethod
    def _release_date(value: date | datetime) -> date:
        return value.date() if isinstance(value, datetime) else value

    @classmethod
    def _scored_movies(cls, today: date) -> list[_ScoredMovie]:
        actor_evidence = cls._history_actor_evidence(today)
        best_by_movie_id: dict[int, _ScoredMovie] = {}
        for movie_id, release_date, actor_id in cls._candidate_rows(today):
            normalized_movie_id = int(movie_id)
            normalized_actor_id = int(actor_id)
            evidence = actor_evidence.get(normalized_actor_id)
            if evidence is None:
                continue

            own_evidence = evidence.by_movie_id.get(normalized_movie_id)
            historical_movie_count = evidence.movie_count - int(own_evidence is not None)
            if historical_movie_count < cls.MIN_HISTORICAL_MOVIES:
                continue

            score = (evidence.total - (own_evidence or 0.0)) / historical_movie_count
            candidate = _ScoredMovie(
                movie_id=normalized_movie_id,
                release_date=release_date,
                actor_id=normalized_actor_id,
                historical_movie_count=historical_movie_count,
                score=score,
            )
            current = best_by_movie_id.get(normalized_movie_id)
            if current is None or (candidate.score, -candidate.actor_id) > (
                current.score,
                -current.actor_id,
            ):
                best_by_movie_id[normalized_movie_id] = candidate

        return sorted(
            best_by_movie_id.values(),
            key=lambda item: (item.score, item.release_date, item.movie_id),
            reverse=True,
        )

    @classmethod
    def _page_resources(
        cls,
        scored_movies: list[_ScoredMovie],
    ) -> list[HotActressReleaseMovieResource]:
        movie_ids = [item.movie_id for item in scored_movies]
        actor_ids = [item.actor_id for item in scored_movies]
        movie_query, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies_by_id = {
            movie.id: movie
            for movie in movie_query.where(Movie.id.in_(movie_ids))
        }
        movies = [movies_by_id[item.movie_id] for item in scored_movies if item.movie_id in movies_by_id]
        MovieRecommendationService._attach_movie_flags(movies)

        actors_by_id = {
            actor.id: actor
            for actor in (
                Actor.select(Actor, Image)
                .join(Image, JOIN.LEFT_OUTER, on=(Actor.profile_image == Image.id))
                .where(Actor.id.in_(actor_ids))
            )
        }
        resources: list[HotActressReleaseMovieResource] = []
        for scored_movie in scored_movies:
            movie = movies_by_id.get(scored_movie.movie_id)
            actor = actors_by_id.get(scored_movie.actor_id)
            if movie is None or actor is None:
                continue
            movie_resource = MovieListItemResource.from_attributes_model(movie)
            actress_resource = HotActressResource.model_validate(
                {
                    "id": actor.id,
                    "name": actor.name,
                    "profile_image": actor.profile_image,
                    "historical_movie_count": scored_movie.historical_movie_count,
                    "hotness_score": round(scored_movie.score, 4),
                }
            )
            resources.append(
                HotActressReleaseMovieResource.model_validate(
                    {
                        **movie_resource.model_dump(),
                        "recommendation_score": round(scored_movie.score, 4),
                        "hot_actress": actress_resource.model_dump(),
                    }
                )
            )
        return resources

    @classmethod
    def list_items(
        cls,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[HotActressReleaseMovieResource]:
        safe_page = int(page)
        safe_page_size = int(page_size)
        validate_page(
            safe_page,
            safe_page_size,
            error_code="invalid_hot_actress_release_filter",
        )
        scored_movies = cls._scored_movies(cls._today())
        start = (safe_page - 1) * safe_page_size
        return PageResponse[HotActressReleaseMovieResource](
            items=cls._page_resources(scored_movies[start : start + safe_page_size]),
            page=safe_page,
            page_size=safe_page_size,
            total=len(scored_movies),
        )
