"""影片相似度推荐服务。

基于影片演员/标签做加权 Jaccard，热度做排序 boost，离线预计算 Top-N 写入
``movie_similarity`` 表。请求接口直接读表，不在请求线程内做计算。
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Iterable, Sequence

from loguru import logger
from peewee import fn

from src.api.exception.errors import ApiError
from src.common import normalize_movie_number
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import parse_special_tags_text, with_movie_card_relations
from src.model import (
    Media,
    Movie,
    MovieActor,
    MovieSimilarity,
    MovieTag,
    get_database,
)
from src.schema.catalog.movies import MovieListItemResource


SIM_WEIGHT_ACTOR = 0.6
SIM_WEIGHT_TAG = 0.4
HEAT_BOOST_ALPHA = 0.3
SIM_TOP_N = 50


class SimilarMovieItem:
    """承载相似影片的中间结果，供 schema 层组装响应。"""

    def __init__(self, movie: Movie, can_play: bool, similarity_score: float) -> None:
        self.movie = movie
        self.can_play = can_play
        self.similarity_score = similarity_score


def _jaccard(a: set[int], b: set[int]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if intersection == 0:
        return 0.0
    union = len(a | b)
    return intersection / union


def _heat_boost(heat: int, heat_ref: float) -> float:
    if heat_ref <= 0:
        return 1.0
    normalized = min(1.0, max(0, heat) / heat_ref)
    return 1.0 + HEAT_BOOST_ALPHA * normalized


def _compute_heat_p95(non_collection_movie_ids: Sequence[int]) -> float:
    """对非合集影片热度求 P95，作为热度归一化分母。"""
    if not non_collection_movie_ids:
        return 0.0
    heats = [
        heat
        for (heat,) in Movie.select(Movie.heat)
        .where(Movie.id.in_(list(non_collection_movie_ids)))
        .tuples()
    ]
    heats = [int(heat or 0) for heat in heats]
    positive_heats = [heat for heat in heats if heat > 0]
    if not positive_heats:
        return 0.0
    positive_heats.sort()
    # 按 P95 取分位数；在数据量很小的极端 case 下回退为最大值。
    index = max(0, math.ceil(0.95 * len(positive_heats)) - 1)
    return float(positive_heats[index])


def _load_actor_groups(movie_ids: Iterable[int]) -> dict[int, set[int]]:
    groups: dict[int, set[int]] = defaultdict(set)
    rows = MovieActor.select(MovieActor.movie, MovieActor.actor).where(
        MovieActor.movie.in_(list(movie_ids))
    ).tuples()
    for movie_id, actor_id in rows:
        groups[movie_id].add(actor_id)
    return groups


def _load_tag_groups(movie_ids: Iterable[int]) -> dict[int, set[int]]:
    groups: dict[int, set[int]] = defaultdict(set)
    rows = MovieTag.select(MovieTag.movie, MovieTag.tag).where(
        MovieTag.movie.in_(list(movie_ids))
    ).tuples()
    for movie_id, tag_id in rows:
        groups[movie_id].add(tag_id)
    return groups


class MovieRecommendationService:
    """影片相似度计算与查询。"""

    @staticmethod
    def _normalized_movie_number_expression():
        normalized = fn.UPPER(fn.TRIM(Movie.movie_number))
        normalized = fn.REPLACE(normalized, " ", "")
        normalized = fn.REPLACE(normalized, "_", "-")
        normalized = fn.REPLACE(normalized, "PPV-", "")
        return normalized

    @staticmethod
    def _attach_movie_flags(movies: Sequence[Movie]) -> None:
        movie_numbers = [movie.movie_number for movie in movies]
        if not movie_numbers:
            return

        playable_movie_numbers: set[str] = set()
        is_4k_movie_numbers: set[str] = set()
        media_rows = (
            Media.select(Media.movie, Media.special_tags)
            .where(
                Media.valid == True,
                Media.movie.in_(movie_numbers),
            )
            .tuples()
        )
        for movie_number, special_tags in media_rows:
            playable_movie_numbers.add(movie_number)
            if "4K" in parse_special_tags_text(special_tags):
                is_4k_movie_numbers.add(movie_number)

        for movie in movies:
            # 相似影片接口也要返回和普通影片列表一致的播放/4K 聚合标记。
            movie.can_play = movie.movie_number in playable_movie_numbers
            movie.is_4k = movie.movie_number in is_4k_movie_numbers

    def compute_for_movie(
        self,
        movie_id: int,
        *,
        heat_ref: float,
        top_n: int = SIM_TOP_N,
    ) -> list[tuple[int, float]]:
        """单部影片的 Top-N 相似列表，返回 [(target_movie_id, score)]。"""
        source_actor_ids: set[int] = {
            actor_id
            for (actor_id,) in MovieActor.select(MovieActor.actor)
            .where(MovieActor.movie == movie_id)
            .tuples()
        }
        source_tag_ids: set[int] = {
            tag_id
            for (tag_id,) in MovieTag.select(MovieTag.tag)
            .where(MovieTag.movie == movie_id)
            .tuples()
        }
        if not source_actor_ids and not source_tag_ids:
            return []

        # 候选裁剪：演员或标签至少有一个交集，并排除合集与自身。
        candidate_movie_ids: set[int] = set()
        if source_actor_ids:
            candidate_movie_ids.update(
                movie_id_
                for (movie_id_,) in MovieActor.select(MovieActor.movie)
                .where(MovieActor.actor.in_(list(source_actor_ids)))
                .tuples()
            )
        if source_tag_ids:
            candidate_movie_ids.update(
                movie_id_
                for (movie_id_,) in MovieTag.select(MovieTag.movie)
                .where(MovieTag.tag.in_(list(source_tag_ids)))
                .tuples()
            )
        candidate_movie_ids.discard(movie_id)
        if not candidate_movie_ids:
            return []

        candidate_rows = list(
            Movie.select(Movie.id, Movie.heat)
            .where(
                Movie.id.in_(list(candidate_movie_ids)),
                Movie.is_collection == False,
            )
            .tuples()
        )
        if not candidate_rows:
            return []
        candidate_ids = [row[0] for row in candidate_rows]
        heat_by_id = {row[0]: int(row[1] or 0) for row in candidate_rows}

        actor_groups = _load_actor_groups(candidate_ids)
        tag_groups = _load_tag_groups(candidate_ids)

        scored: list[tuple[int, float]] = []
        for candidate_id in candidate_ids:
            base_sim = (
                SIM_WEIGHT_ACTOR * _jaccard(source_actor_ids, actor_groups.get(candidate_id, set()))
                + SIM_WEIGHT_TAG * _jaccard(source_tag_ids, tag_groups.get(candidate_id, set()))
            )
            if base_sim <= 0:
                continue
            final_score = base_sim * _heat_boost(heat_by_id.get(candidate_id, 0), heat_ref)
            scored.append((candidate_id, final_score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_n]

    def replace_similarity_rows(
        self,
        source_movie_id: int,
        ranked: list[tuple[int, float]],
    ) -> int:
        """事务内删旧 + 批插新，保证 source 的 Top-N 切换原子化。"""
        now = utc_now_for_db()
        with get_database().atomic():
            MovieSimilarity.delete().where(
                MovieSimilarity.source_movie == source_movie_id
            ).execute()
            if not ranked:
                return 0
            rows = [
                {
                    "source_movie": source_movie_id,
                    "target_movie": target_id,
                    "score": float(score),
                    "rank": rank,
                    "created_at": now,
                    "updated_at": now,
                }
                for rank, (target_id, score) in enumerate(ranked, start=1)
            ]
            MovieSimilarity.insert_many(rows).execute()
            return len(rows)

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    @staticmethod
    def _purge_collection_source_rows() -> int:
        # 合集影片不会参与 source 侧推荐，重算前先清掉历史遗留结果，避免接口读到陈旧数据。
        collection_source_ids = Movie.select(Movie.id).where(Movie.is_collection == True)
        return (
            MovieSimilarity.delete()
            .where(MovieSimilarity.source_movie.in_(collection_source_ids))
            .execute()
        )

    def recompute_all(
        self,
        *,
        top_n: int = SIM_TOP_N,
        progress_callback=None,
    ) -> dict[str, int]:
        """全量重算：遍历所有非合集影片，重写 movie_similarity 表。"""
        non_collection_ids = [
            movie_id
            for (movie_id,) in Movie.select(Movie.id)
            .where(Movie.is_collection == False)
            .tuples()
        ]
        stats = {
            "total_movies": len(non_collection_ids),
            "processed_movies": 0,
            "stored_pairs": 0,
            "skipped_movies": 0,
        }
        self._emit_progress(
            progress_callback,
            current=0,
            total=stats["total_movies"],
            text="开始计算影片相似度",
            summary_patch=stats,
        )
        if not non_collection_ids:
            return stats

        deleted_collection_source_rows = self._purge_collection_source_rows()
        heat_ref = _compute_heat_p95(non_collection_ids)
        logger.info(
            "movie similarity recompute started total_movies={} heat_ref={} deleted_collection_source_rows={}",
            stats["total_movies"],
            heat_ref,
            deleted_collection_source_rows,
        )

        for movie_id in non_collection_ids:
            try:
                ranked = self.compute_for_movie(
                    movie_id,
                    heat_ref=heat_ref,
                    top_n=top_n,
                )
                stored = self.replace_similarity_rows(movie_id, ranked)
                stats["stored_pairs"] += stored
                if stored == 0:
                    stats["skipped_movies"] += 1
            except Exception as exc:
                # 单部失败仅记录，不能阻塞整体重算。
                stats["skipped_movies"] += 1
                logger.warning(
                    "movie similarity compute failed movie_id={} detail={}",
                    movie_id,
                    exc,
                )
            stats["processed_movies"] += 1
            if stats["processed_movies"] % 200 == 0:
                self._emit_progress(
                    progress_callback,
                    current=stats["processed_movies"],
                    total=stats["total_movies"],
                    text=f"已处理 {stats['processed_movies']}/{stats['total_movies']}",
                    summary_patch=stats,
                )

        self._emit_progress(
            progress_callback,
            current=stats["processed_movies"],
            total=stats["total_movies"],
            text="影片相似度重算完成",
            summary_patch=stats,
        )
        logger.info(
            "movie similarity recompute finished processed_movies={} stored_pairs={} skipped_movies={}",
            stats["processed_movies"],
            stats["stored_pairs"],
            stats["skipped_movies"],
        )
        return stats

    def list_similar(
        self,
        movie_number: str,
        limit: int = 20,
    ) -> list[SimilarMovieItem]:
        """对外查询：按 movie_number 解出 source，取 Top-N 已落表结果。"""
        normalized_number = normalize_movie_number(movie_number)
        if not normalized_number:
            raise ApiError(
                404,
                "movie_not_found",
                "影片不存在",
                {"movie_number": movie_number},
            )

        source_movie = (
            Movie.select(Movie)
            .where(self._normalized_movie_number_expression() == normalized_number)
            .get_or_none()
        )
        if source_movie is None:
            raise ApiError(
                404,
                "movie_not_found",
                "影片不存在",
                {"movie_number": movie_number},
            )

        safe_limit = max(int(limit), 0)
        if safe_limit == 0:
            return []

        similarity_rows = list(
            MovieSimilarity.select()
            .where(MovieSimilarity.source_movie == source_movie.id)
            .order_by(MovieSimilarity.rank.asc())
            .limit(safe_limit)
        )
        if not similarity_rows:
            return []

        target_ids = [row.target_movie_id for row in similarity_rows]
        score_by_target_id = {row.target_movie_id: row.score for row in similarity_rows}
        movie_query, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies_by_id = {
            movie.id: movie
            for movie in movie_query.where(Movie.id.in_(target_ids))
        }
        self._attach_movie_flags(list(movies_by_id.values()))

        items: list[SimilarMovieItem] = []
        for target_id in target_ids:
            movie = movies_by_id.get(target_id)
            if movie is None:
                continue
            items.append(
                SimilarMovieItem(
                    movie=movie,
                    can_play=bool(getattr(movie, "can_play", False)),
                    similarity_score=score_by_target_id.get(target_id, 0.0),
                )
            )
        return items

    def list_similar_resources(
        self,
        movie_number: str,
        limit: int = 20,
    ):
        """组装响应：复用 MovieListItemResource，附加 similarity_score 字段。"""
        from src.schema.catalog.movies import SimilarMovieListItemResource

        items = self.list_similar(movie_number=movie_number, limit=limit)
        resources: list[SimilarMovieListItemResource] = []
        for item in items:
            base_resource = MovieListItemResource.from_attributes_model(item.movie)
            base_resource.can_play = item.can_play
            resources.append(
                SimilarMovieListItemResource.model_validate(
                    {
                        **base_resource.model_dump(),
                        "similarity_score": item.similarity_score,
                    }
                )
            )
        return resources
