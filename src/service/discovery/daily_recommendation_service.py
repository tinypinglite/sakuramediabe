from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from datetime import date, datetime, time

from loguru import logger
from peewee import JOIN

from src.common.runtime_time import runtime_now, utc_now_for_db
from src.common.service_helpers import (
    emit_progress,
    validate_page,
    with_movie_card_relations,
)
from src.model import (
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Actor,
    DailyRecommendationItem,
    HotReviewItem,
    Movie,
    MovieActor,
    Playlist,
    PlaylistMovie,
    RankingItem,
    get_database,
)
from src.schema.catalog.movies import MovieListItemResource
from src.schema.common.pagination import PageResponse
from src.schema.discovery import DailyRecommendationMovieResource
from src.service.discovery.qdrant_movie_similarity_store import (
    MovieSimilarityIndexError,
)
from src.service.discovery.recommendation_service import MovieRecommendationService

DAILY_RECOMMENDATION_LIMIT = 200
RECENT_SEED_LIMIT = 30
SIMILARITY_PER_SEED_LIMIT = 50
RANK_DECAY_WINDOW = 100

REGULAR_WEIGHTS = {
    "similarity": 0.40,
    "subscribed_actor": 0.15,
    "subscribed_movie": 0.10,
    "heat": 0.20,
    "ranking": 0.10,
    "hot_review": 0.05,
}
COLD_START_WEIGHTS = {
    "heat": 0.55,
    "ranking": 0.25,
    "hot_review": 0.10,
    "freshness": 0.10,
}

REASON_TEXTS = {
    "similar_recent_play": "与你最近播放的影片相似",
    "subscribed_actor": "包含已订阅演员",
    "subscribed_movie": "你已订阅这部影片",
    "popular_movie": "近期热度较高",
    "ranking_trending": "来自近期榜单",
    "hot_review": "近期热评活跃",
    "new_release": "较新发布或近期入库",
}


class _CandidateMovie:
    """每日推荐候选的轻量评分输入：只驻留打分所需字段，不持有完整 Movie 模型。

    生成链路（30 万级全库候选）不再把完整 Movie 对象全量加载进内存，
    评分与落库只用 id / heat / release_date / created_at。
    """

    __slots__ = ("created_at", "heat", "id", "is_subscribed", "release_date")

    def __init__(
        self,
        *,
        id: int,
        heat: int,
        release_date,
        created_at,
        is_subscribed: bool = False,
    ) -> None:
        self.id = id
        self.heat = heat
        self.release_date = release_date
        self.created_at = created_at
        self.is_subscribed = is_subscribed


class _ScoredRecommendation:
    def __init__(
        self,
        movie: _CandidateMovie,
        score: float,
        reason_codes: list[str],
        signal_scores: dict[str, float],
    ) -> None:
        self.movie = movie
        self.score = score
        self.reason_codes = reason_codes
        self.signal_scores = signal_scores


class DailyRecommendationService:
    """每日推荐快照生成与查询。"""

    @staticmethod
    def _snapshot_date(target_date: date | None) -> date:
        return target_date or runtime_now().date()

    @staticmethod
    def _normalize(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    @staticmethod
    def _rank_decay(rank: int, *, weight: float = 1.0) -> float:
        if rank <= 0 or rank > RANK_DECAY_WINDOW:
            return 0.0
        return max(0.0, (1.0 - ((rank - 1) / RANK_DECAY_WINDOW)) * weight)

    @staticmethod
    def _release_sort_value(movie: _CandidateMovie) -> datetime:
        release_date = movie.release_date
        if release_date is None:
            return datetime.min
        if isinstance(release_date, datetime):
            return release_date
        return datetime.combine(release_date, time.min)

    @classmethod
    def _load_candidate_movies(cls) -> list[_CandidateMovie]:
        # 全库候选只投影打分所需列，避免 30 万完整 Movie 模型驻留内存（实测峰值 3.9GB）。
        # is_collection 仅用于过滤，不需要进入投影列。
        rows = (
            Movie.select(
                Movie.id,
                Movie.heat,
                Movie.release_date,
                Movie.created_at,
                Movie.is_subscribed,
            )
            .where(Movie.is_collection == False)
            .order_by(Movie.id.asc())
        )
        return [
            _CandidateMovie(
                id=row.id,
                heat=int(row.heat or 0),
                release_date=row.release_date,
                created_at=row.created_at,
                is_subscribed=bool(row.is_subscribed),
            )
            for row in rows
        ]

    @staticmethod
    def _load_recent_seed_ids(candidate_ids: set[int]) -> list[int]:
        if not candidate_ids:
            return []
        playlist = Playlist.get_or_none(Playlist.kind == PLAYLIST_KIND_RECENTLY_PLAYED)
        if playlist is None:
            return []
        rows = (
            PlaylistMovie.select(PlaylistMovie.movie)
            .where(PlaylistMovie.playlist == playlist, PlaylistMovie.movie.in_(candidate_ids))
            .order_by(PlaylistMovie.updated_at.desc(), PlaylistMovie.id.desc())
            .limit(RECENT_SEED_LIMIT)
        )
        return [row.movie_id for row in rows]

    @staticmethod
    def _load_subscribed_actor_movie_ids(candidate_ids: set[int]) -> set[int]:
        if not candidate_ids:
            return set()
        rows = (
            MovieActor.select(MovieActor.movie)
            .join(Actor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            .where(Actor.is_subscribed == True, MovieActor.movie.in_(candidate_ids))
            .tuples()
        )
        return {movie_id for (movie_id,) in rows}

    @classmethod
    def _load_similarity_scores(cls, seed_ids: Sequence[int], candidate_ids: set[int]) -> dict[int, float]:
        if not seed_ids or not candidate_ids:
            return {}
        seed_weight_by_id = {
            seed_id: 1.0 - (index / max(len(seed_ids), 1))
            for index, seed_id in enumerate(seed_ids)
        }
        try:
            hits_by_seed_id = MovieRecommendationService().search_similar_movies(
                seed_ids,
                limit=SIMILARITY_PER_SEED_LIMIT,
            )
        except MovieSimilarityIndexError as exc:
            # 每日推荐还有热度、榜单等信号，Qdrant 故障时只跳过相似度信号。
            logger.warning("每日推荐跳过影片相似度信号 detail={}", exc)
            return {}
        scores: dict[int, float] = defaultdict(float)
        for seed_id, hits in hits_by_seed_id.items():
            seed_weight = seed_weight_by_id.get(seed_id, 0.0)
            for hit in hits:
                if hit.movie_id not in candidate_ids:
                    continue
                score = cls._normalize(hit.score * seed_weight)
                scores[hit.movie_id] = max(scores[hit.movie_id], score)
        return dict(scores)

    @classmethod
    def _load_heat_scores(cls, movies: Sequence[_CandidateMovie]) -> dict[int, float]:
        positive_heats = sorted(movie.heat for movie in movies if movie.heat > 0)
        if not positive_heats:
            return {}
        index = max(0, math.ceil(len(positive_heats) * 0.95) - 1)
        heat_ref = max(float(positive_heats[index]), 1.0)
        return {movie.id: cls._normalize(movie.heat / heat_ref) for movie in movies}

    @classmethod
    def _load_ranking_scores(cls, candidate_ids: set[int]) -> dict[int, float]:
        if not candidate_ids:
            return {}
        period_weights = {"daily": 1.0, "weekly": 0.7, "monthly": 0.4, "": 0.7}
        rows = RankingItem.select(RankingItem.movie, RankingItem.rank, RankingItem.period).where(
            RankingItem.movie.in_(candidate_ids)
        )
        scores: dict[int, float] = defaultdict(float)
        for row in rows:
            weight = period_weights.get((row.period or "").lower(), 0.5)
            scores[row.movie_id] = max(
                scores[row.movie_id],
                cls._rank_decay(int(row.rank or 0), weight=weight),
            )
        return dict(scores)

    @classmethod
    def _load_hot_review_scores(cls, candidate_ids: set[int]) -> dict[int, float]:
        if not candidate_ids:
            return {}
        rows = HotReviewItem.select(HotReviewItem.movie, HotReviewItem.rank).where(
            HotReviewItem.movie.in_(candidate_ids)
        )
        scores: dict[int, float] = defaultdict(float)
        for row in rows:
            scores[row.movie_id] = max(scores[row.movie_id], cls._rank_decay(int(row.rank or 0)))
        return dict(scores)

    @classmethod
    def _build_freshness_scores(cls, movies: Sequence[_CandidateMovie]) -> dict[int, float]:
        if not movies:
            return {}
        ordered = sorted(
            movies,
            key=lambda movie: (
                cls._release_sort_value(movie),
                movie.created_at or datetime.min,
                movie.id,
            ),
            reverse=True,
        )
        if len(ordered) == 1:
            return {ordered[0].id: 1.0}
        denominator = max(len(ordered) - 1, 1)
        return {
            movie.id: cls._normalize(1.0 - (index / denominator))
            for index, movie in enumerate(ordered)
        }

    @staticmethod
    def _reason_texts(reason_codes: Sequence[str]) -> list[str]:
        return [REASON_TEXTS[reason_code] for reason_code in reason_codes if reason_code in REASON_TEXTS]

    @classmethod
    def _score_movies(cls, movies: Sequence[_CandidateMovie]) -> tuple[list[_ScoredRecommendation], dict[str, int | bool]]:
        candidate_ids = {movie.id for movie in movies}
        recent_seed_ids = cls._load_recent_seed_ids(candidate_ids)
        subscribed_actor_movie_ids = cls._load_subscribed_actor_movie_ids(candidate_ids)
        # 订阅影片标记已随候选投影加载（Movie.is_subscribed），无需再查一次 30 万行范围。
        subscribed_movie_ids = {movie.id for movie in movies if movie.is_subscribed}
        similarity_scores = cls._load_similarity_scores(recent_seed_ids, candidate_ids)
        heat_scores = cls._load_heat_scores(movies)
        ranking_scores = cls._load_ranking_scores(candidate_ids)
        hot_review_scores = cls._load_hot_review_scores(candidate_ids)
        freshness_scores = cls._build_freshness_scores(movies)

        has_interest_signal = bool(recent_seed_ids or subscribed_actor_movie_ids or subscribed_movie_ids)
        has_public_signal = any(
            score > 0
            for score_map in (heat_scores, ranking_scores, hot_review_scores)
            for score in score_map.values()
        )
        extreme_cold_start = not has_interest_signal and not has_public_signal

        scored: list[_ScoredRecommendation] = []
        for movie in movies:
            signal_scores = {
                "similarity": cls._normalize(similarity_scores.get(movie.id, 0.0)),
                "subscribed_actor": 1.0 if movie.id in subscribed_actor_movie_ids else 0.0,
                "subscribed_movie": 1.0 if movie.id in subscribed_movie_ids else 0.0,
                "heat": cls._normalize(heat_scores.get(movie.id, 0.0)),
                "ranking": cls._normalize(ranking_scores.get(movie.id, 0.0)),
                "hot_review": cls._normalize(hot_review_scores.get(movie.id, 0.0)),
                "freshness": cls._normalize(freshness_scores.get(movie.id, 0.0)),
            }
            if has_interest_signal:
                score = sum(signal_scores[key] * weight for key, weight in REGULAR_WEIGHTS.items())
            elif extreme_cold_start:
                score = signal_scores["freshness"]
            else:
                score = sum(signal_scores[key] * weight for key, weight in COLD_START_WEIGHTS.items())

            reason_codes: list[str] = []
            if signal_scores["similarity"] > 0:
                reason_codes.append("similar_recent_play")
            if signal_scores["subscribed_actor"] > 0:
                reason_codes.append("subscribed_actor")
            if signal_scores["subscribed_movie"] > 0:
                reason_codes.append("subscribed_movie")
            if signal_scores["heat"] > 0:
                reason_codes.append("popular_movie")
            if signal_scores["ranking"] > 0:
                reason_codes.append("ranking_trending")
            if signal_scores["hot_review"] > 0:
                reason_codes.append("hot_review")
            if extreme_cold_start or (not has_interest_signal and signal_scores["freshness"] > 0):
                reason_codes.append("new_release")

            scored.append(_ScoredRecommendation(movie, float(score), reason_codes, signal_scores))

        scored.sort(
            key=lambda item: (
                item.score,
                cls._release_sort_value(item.movie),
                item.movie.created_at or datetime.min,
                item.movie.id,
            ),
            reverse=True,
        )
        stats = {
            "cold_start": not has_interest_signal,
            "extreme_cold_start": extreme_cold_start,
            "recent_seed_movies": len(recent_seed_ids),
            "candidate_movies": len(movies),
        }
        return scored, stats

    @classmethod
    def generate_latest_snapshot(
        cls,
        target_date: date | None = None,
        limit: int = DAILY_RECOMMENDATION_LIMIT,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> dict[str, int | str | bool]:
        snapshot_date = cls._snapshot_date(target_date)
        safe_limit = max(int(limit), 0)
        movies = cls._load_candidate_movies()
        scored, score_stats = cls._score_movies(movies)
        ranked = scored[:safe_limit]
        generated_at = utc_now_for_db()

        with get_database().atomic():
            DailyRecommendationItem.delete().execute()
            rows = [
                {
                    "snapshot_date": snapshot_date,
                    "movie": item.movie.id,
                    "rank": rank,
                    "score": item.score,
                    "reason_codes": item.reason_codes,
                    "reason_texts": cls._reason_texts(item.reason_codes),
                    "signal_scores": item.signal_scores,
                    "generated_at": generated_at,
                    "created_at": generated_at,
                    "updated_at": generated_at,
                }
                for rank, item in enumerate(ranked, start=1)
            ]
            if rows:
                DailyRecommendationItem.insert_many(rows).execute()

        stats = {
            "snapshot_date": snapshot_date.isoformat(),
            "candidate_movies": int(score_stats["candidate_movies"]),
            "stored_items": len(ranked),
            "cold_start": bool(score_stats["cold_start"]),
            "extreme_cold_start": bool(score_stats["extreme_cold_start"]),
            "recent_seed_movies": int(score_stats["recent_seed_movies"]),
        }
        emit_progress(progress_callback, **stats)
        return stats

    @classmethod
    def list_items(
        cls,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[DailyRecommendationMovieResource]:
        safe_page = int(page)
        safe_page_size = int(page_size)
        validate_page(safe_page, safe_page_size, error_code="invalid_daily_recommendation_filter")
        start = (safe_page - 1) * safe_page_size
        total = DailyRecommendationItem.select().count()
        rows = list(
            DailyRecommendationItem.select()
            .order_by(DailyRecommendationItem.rank.asc())
            .offset(start)
            .limit(safe_page_size)
        )
        if not rows:
            return PageResponse[DailyRecommendationMovieResource](
                items=[],
                page=safe_page,
                page_size=safe_page_size,
                total=total,
            )

        movie_ids = [row.movie_id for row in rows]
        movie_query, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies_by_id = {movie.id: movie for movie in movie_query.where(Movie.id.in_(movie_ids))}
        MovieRecommendationService._attach_movie_flags(list(movies_by_id.values()))
        today = runtime_now().date()

        items: list[DailyRecommendationMovieResource] = []
        for row in rows:
            movie = movies_by_id.get(row.movie_id)
            if movie is None:
                continue
            base_resource = MovieListItemResource.from_attributes_model(movie)
            base_resource.can_play = bool(getattr(movie, "can_play", False))
            base_resource.is_4k = bool(getattr(movie, "is_4k", False))
            items.append(
                DailyRecommendationMovieResource.model_validate(
                    {
                        **base_resource.model_dump(),
                        "snapshot_date": row.snapshot_date,
                        "generated_at": row.generated_at,
                        "rank": row.rank,
                        "recommendation_score": row.score,
                        "reason_codes": row.reason_codes or [],
                        "reason_texts": row.reason_texts or [],
                        "signal_scores": row.signal_scores or {},
                        "is_stale": row.snapshot_date < today,
                    }
                )
            )
        return PageResponse[DailyRecommendationMovieResource](
            items=items,
            page=safe_page,
            page_size=safe_page_size,
            total=total,
        )
