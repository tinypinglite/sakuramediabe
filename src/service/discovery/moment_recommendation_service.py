from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from loguru import logger

from src.common import resolve_image_file_path
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import validate_page, with_movie_card_relations
from src.model import (
    Image,
    Media,
    MediaPoint,
    MediaThumbnail,
    MomentRecommendation,
    Movie,
    get_database,
)
from src.schema.catalog.actors import ImageResource
from src.schema.catalog.movies import MovieListItemResource
from src.schema.discovery import (
    MomentRecommendationItemResource,
    MomentRecommendationPageResource,
)
from src.service.discovery.joytag_embedder_client import (
    JoyTagInferenceClientError,
    get_joytag_embedder_client,
)
from src.service.discovery.qdrant_movie_similarity_store import (
    MovieSimilarityIndexError,
)
from src.service.discovery.qdrant_thumbnail_store import (
    QdrantThumbnailStore,
    get_qdrant_thumbnail_store,
)
from src.service.discovery.recommendation_service import MovieRecommendationService

MOMENT_RECOMMENDATION_LIMIT = 300
MOMENT_RECOMMENDATION_SEED_LIMIT = 30
VISUAL_SEARCH_PER_SEED_LIMIT = 40
SIMILAR_MOVIE_PER_SEED_LIMIT = 50
MAX_RECOMMENDATIONS_PER_MOVIE = 3
POPULAR_TARGET_RATIO = 0.35
STRATEGY_VISUAL = "visual"
STRATEGY_SIMILAR_MOVIE = "similar_movie"
STRATEGY_POPULAR = "popular"
STRATEGY_PRIORITY = {
    STRATEGY_VISUAL: 0,
    STRATEGY_SIMILAR_MOVIE: 1,
    STRATEGY_POPULAR: 2,
}
REASON_TEXTS = {
    STRATEGY_VISUAL: "与你收藏的时刻画面相似",
    STRATEGY_SIMILAR_MOVIE: "来自相似影片的相近时刻",
    STRATEGY_POPULAR: "来自热门影片的精选时刻",
}


@dataclass(frozen=True)
class _MomentSeed:
    point: MediaPoint
    thumbnail: MediaThumbnail
    media: Media
    movie: Movie
    recency_score: float


@dataclass
class _MomentCandidate:
    thumbnail: MediaThumbnail
    media: Media
    movie: Movie
    score: float
    strategy: str
    reason: str
    seed_point_id: int | None = None
    seed_thumbnail_id: int | None = None
    source_movie_id: int | None = None
    visual_score: float | None = None
    movie_similarity_score: float | None = None


class MomentRecommendationService:
    """推荐时刻生成与当前推荐池查询。"""

    def __init__(
        self,
        *,
        store: QdrantThumbnailStore | None = None,
        embedder=None,
        movie_recommendation_service: MovieRecommendationService | None = None,
    ) -> None:
        self.store = store or get_qdrant_thumbnail_store()
        self.embedder = embedder or get_joytag_embedder_client()
        self.movie_recommendation_service = (
            movie_recommendation_service or MovieRecommendationService()
        )

    @staticmethod
    def _emit_progress(progress_callback: Callable[[dict], None] | None, **payload) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    @staticmethod
    def _heat_score(movie: Movie) -> float:
        return max(0.0, min(1.0, float(movie.heat or 0) / 100.0))

    @staticmethod
    def _safe_ratio(offset_seconds: int, duration_seconds: int | None) -> float | None:
        duration = int(duration_seconds or 0)
        if duration <= 0:
            return None
        return max(0.0, min(1.0, float(offset_seconds) / float(duration)))

    @staticmethod
    def _thumbnail_query():
        return (
            MediaThumbnail.select(MediaThumbnail, Image, Media, Movie)
            .join(Image)
            .switch(MediaThumbnail)
            .join(Media)
            .join(Movie, on=(Media.movie == Movie.movie_number))
        )

    @classmethod
    def _get_thumbnails_by_ids(cls, thumbnail_ids: Sequence[int]) -> dict[int, MediaThumbnail]:
        if not thumbnail_ids:
            return {}
        unique_ids = [int(item) for item in dict.fromkeys(thumbnail_ids)]
        rows = cls._thumbnail_query().where(MediaThumbnail.id.in_(unique_ids), Media.valid == True)
        return {int(row.id): row for row in rows}

    @classmethod
    def _load_seeds(cls, limit: int = MOMENT_RECOMMENDATION_SEED_LIMIT) -> list[_MomentSeed]:
        rows = list(
            MediaPoint.select(MediaPoint, Media, Movie, MediaThumbnail, Image)
            .join(Media)
            .join(Movie, on=(Media.movie == Movie.movie_number))
            .switch(MediaPoint)
            .join(MediaThumbnail)
            .switch(MediaThumbnail)
            .join(Image)
            .where(Media.valid == True)
            .order_by(MediaPoint.created_at.desc(), MediaPoint.id.desc())
            .limit(max(int(limit), 0))
        )
        total = len(rows)
        seeds: list[_MomentSeed] = []
        for index, point in enumerate(rows):
            recency_score = 1.0 if total <= 1 else 1.0 - (index / total)
            seeds.append(
                _MomentSeed(
                    point=point,
                    thumbnail=point.thumbnail,
                    media=point.media,
                    movie=point.media.movie,
                    recency_score=recency_score,
                )
            )
        return seeds

    @staticmethod
    def _read_seed_image_bytes(seed: _MomentSeed) -> bytes | None:
        try:
            image_path = resolve_image_file_path(seed.thumbnail.image.origin)
            if not image_path.exists() or not image_path.is_file():
                return None
            return image_path.read_bytes()
        except Exception as exc:
            logger.warning("Moment recommendation seed image skipped point_id={} detail={}", seed.point.id, exc)
            return None

    def _infer_seed_vector(self, seed: _MomentSeed) -> list[float] | None:
        image_bytes = self._read_seed_image_bytes(seed)
        if not image_bytes:
            return None
        try:
            inference = self.embedder.infer_image_bytes(image_bytes)
        except (JoyTagInferenceClientError, ValueError) as exc:
            logger.warning("Moment recommendation JoyTag inference skipped point_id={} detail={}", seed.point.id, exc)
            return None
        return [float(item) for item in inference.vector]

    @classmethod
    def _add_candidate(
        cls,
        candidates_by_thumbnail_id: dict[int, _MomentCandidate],
        candidate: _MomentCandidate,
    ) -> None:
        existing = candidates_by_thumbnail_id.get(candidate.thumbnail.id)
        if existing is None:
            candidates_by_thumbnail_id[candidate.thumbnail.id] = candidate
            return
        existing_key = (existing.score, -STRATEGY_PRIORITY.get(existing.strategy, 99))
        candidate_key = (candidate.score, -STRATEGY_PRIORITY.get(candidate.strategy, 99))
        if candidate_key > existing_key:
            candidates_by_thumbnail_id[candidate.thumbnail.id] = candidate

    def _collect_visual_candidates(
        self,
        seeds: Sequence[_MomentSeed],
        candidates_by_thumbnail_id: dict[int, _MomentCandidate],
    ) -> int:
        added_count = 0
        for seed in seeds:
            query_vector = self._infer_seed_vector(seed)
            if not query_vector:
                continue
            try:
                hits = self.store.search(
                    query_vector,
                    limit=VISUAL_SEARCH_PER_SEED_LIMIT,
                    exclude_movie_ids=[seed.movie.id],
                )
            except Exception as exc:
                logger.warning("Moment recommendation vector search skipped point_id={} detail={}", seed.point.id, exc)
                continue
            thumbnails_by_id = self._get_thumbnails_by_ids([hit.thumbnail_id for hit in hits])
            for hit in hits:
                if hit.thumbnail_id == seed.thumbnail.id:
                    continue
                thumbnail = thumbnails_by_id.get(hit.thumbnail_id)
                if thumbnail is None or not thumbnail.media.valid:
                    continue
                media = thumbnail.media
                movie = media.movie
                # 推荐时刻只面向单部影片，视觉召回也要排除合集条目。
                if movie.is_collection:
                    continue
                # 双层排除种子所属影片，避免旧索引或测试替身漏传过滤条件时引入同片时刻。
                if movie.id == seed.movie.id:
                    continue
                score = 0.75 * float(hit.score) + 0.15 * self._heat_score(movie) + 0.10 * seed.recency_score
                before_count = len(candidates_by_thumbnail_id)
                self._add_candidate(
                    candidates_by_thumbnail_id,
                    _MomentCandidate(
                        thumbnail=thumbnail,
                        media=media,
                        movie=movie,
                        score=score,
                        strategy=STRATEGY_VISUAL,
                        reason=REASON_TEXTS[STRATEGY_VISUAL],
                        seed_point_id=seed.point.id,
                        seed_thumbnail_id=seed.thumbnail.id,
                        source_movie_id=seed.movie.id,
                        visual_score=float(hit.score),
                    ),
                )
                if len(candidates_by_thumbnail_id) > before_count:
                    added_count += 1
        return added_count

    @staticmethod
    def _choose_thumbnail_from_media_thumbnails(
        media: Media,
        thumbnails: Sequence[MediaThumbnail],
        target_ratio: float | None,
    ) -> MediaThumbnail | None:
        if not thumbnails:
            return None
        ratio = POPULAR_TARGET_RATIO if target_ratio is None else target_ratio
        desired_offset = int((media.duration_seconds or 0) * ratio)
        if desired_offset <= 0:
            desired_offset = int(thumbnails[len(thumbnails) // 2].offset)
        return min(thumbnails, key=lambda item: (abs(int(item.offset) - desired_offset), item.id))

    @classmethod
    def _choose_thumbnail_for_media(cls, media: Media, target_ratio: float | None) -> MediaThumbnail | None:
        thumbnails = list(
            cls._thumbnail_query()
            .where(MediaThumbnail.media == media, Media.valid == True)
            .order_by(MediaThumbnail.offset.asc(), MediaThumbnail.id.asc())
        )
        return cls._choose_thumbnail_from_media_thumbnails(media, thumbnails, target_ratio)

    @classmethod
    def _choose_thumbnail_for_movie(cls, movie_id: int, target_ratio: float | None) -> tuple[Media, MediaThumbnail] | None:
        thumbnails = list(
            cls._thumbnail_query()
            .where(Movie.id == movie_id, Media.valid == True)
            .order_by(Media.id.asc(), MediaThumbnail.offset.asc(), MediaThumbnail.id.asc())
        )
        thumbnails_by_media_id: dict[int, list[MediaThumbnail]] = {}
        for thumbnail in thumbnails:
            thumbnails_by_media_id.setdefault(thumbnail.media.id, []).append(thumbnail)

        candidates: list[tuple[int, int, int, Media, MediaThumbnail]] = []
        for media_thumbnails in thumbnails_by_media_id.values():
            media = media_thumbnails[0].media
            thumbnail = cls._choose_thumbnail_from_media_thumbnails(media, media_thumbnails, target_ratio)
            if thumbnail is None:
                continue
            # 按每条媒体自身时长计算目标时间点，避免第一条媒体缺缩略图时误跳过整部影片。
            ratio = POPULAR_TARGET_RATIO if target_ratio is None else target_ratio
            desired_offset = int((media.duration_seconds or 0) * ratio)
            if desired_offset <= 0:
                desired_offset = int(thumbnail.offset)
            candidates.append((abs(int(thumbnail.offset) - desired_offset), media.id, thumbnail.id, media, thumbnail))
        if not candidates:
            return None
        _distance, _media_id, _thumbnail_id, media, thumbnail = min(candidates, key=lambda item: item[:3])
        return media, thumbnail

    def _collect_similar_movie_candidates(
        self,
        seeds: Sequence[_MomentSeed],
        candidates_by_thumbnail_id: dict[int, _MomentCandidate],
    ) -> int:
        try:
            hits_by_seed_id = self.movie_recommendation_service.search_similar_movies(
                [seed.movie.id for seed in seeds],
                limit=SIMILAR_MOVIE_PER_SEED_LIMIT,
            )
        except MovieSimilarityIndexError as exc:
            # 时刻推荐仍可由视觉相似与热门候选完成，Qdrant 故障不阻塞整池生成。
            logger.warning("时刻推荐跳过影片相似度信号 detail={}", exc)
            return 0

        added_count = 0
        for seed in seeds:
            target_ratio = self._safe_ratio(seed.point.offset_seconds, seed.media.duration_seconds)
            for hit in hits_by_seed_id.get(seed.movie.id, []):
                selected = self._choose_thumbnail_for_movie(hit.movie_id, target_ratio)
                if selected is None:
                    continue
                media, thumbnail = selected
                movie = media.movie
                if movie.is_collection:
                    continue
                score = 0.65 * hit.score + 0.20 * self._heat_score(movie) + 0.15 * seed.recency_score
                before_count = len(candidates_by_thumbnail_id)
                self._add_candidate(
                    candidates_by_thumbnail_id,
                    _MomentCandidate(
                        thumbnail=thumbnail,
                        media=media,
                        movie=movie,
                        score=score,
                        strategy=STRATEGY_SIMILAR_MOVIE,
                        reason=REASON_TEXTS[STRATEGY_SIMILAR_MOVIE],
                        seed_point_id=seed.point.id,
                        seed_thumbnail_id=seed.thumbnail.id,
                        source_movie_id=seed.movie.id,
                        movie_similarity_score=hit.score,
                    ),
                )
                if len(candidates_by_thumbnail_id) > before_count:
                    added_count += 1
        return added_count

    def _collect_popular_candidates(
        self,
        candidates_by_thumbnail_id: dict[int, _MomentCandidate],
        limit: int,
    ) -> int:
        added_count = 0
        movies = list(
            Movie.select()
            .where(Movie.is_collection == False)
            .order_by(Movie.heat.desc(), Movie.created_at.desc(), Movie.id.desc())
            .limit(max(limit * 5, limit, 1))
        )
        for movie in movies:
            selected = self._choose_thumbnail_for_movie(movie.id, POPULAR_TARGET_RATIO)
            if selected is None:
                continue
            media, thumbnail = selected
            score = 0.60 * self._heat_score(movie)
            before_count = len(candidates_by_thumbnail_id)
            self._add_candidate(
                candidates_by_thumbnail_id,
                _MomentCandidate(
                    thumbnail=thumbnail,
                    media=media,
                    movie=movie,
                    score=score,
                    strategy=STRATEGY_POPULAR,
                    reason=REASON_TEXTS[STRATEGY_POPULAR],
                ),
            )
            if len(candidates_by_thumbnail_id) > before_count:
                added_count += 1
            if len(candidates_by_thumbnail_id) >= limit:
                break
        return added_count

    @staticmethod
    def _rank_candidates(candidates: Sequence[_MomentCandidate], limit: int) -> list[_MomentCandidate]:
        ordered = sorted(
            candidates,
            key=lambda item: (
                -item.score,
                STRATEGY_PRIORITY.get(item.strategy, 99),
                -(item.movie.heat or 0),
                item.thumbnail.id,
            ),
        )
        per_movie_counts: dict[int, int] = {}
        ranked: list[_MomentCandidate] = []
        for candidate in ordered:
            movie_count = per_movie_counts.get(candidate.movie.id, 0)
            if movie_count >= MAX_RECOMMENDATIONS_PER_MOVIE:
                continue
            ranked.append(candidate)
            per_movie_counts[candidate.movie.id] = movie_count + 1
            if len(ranked) >= limit:
                break
        return ranked

    def generate_recommendations(
        self,
        *,
        limit: int = MOMENT_RECOMMENDATION_LIMIT,
        progress_callback: Callable[[dict], None] | None = None,
    ) -> dict[str, int]:
        safe_limit = max(int(limit), 0)
        seeds = self._load_seeds()
        candidates_by_thumbnail_id: dict[int, _MomentCandidate] = {}
        visual_candidates = self._collect_visual_candidates(seeds, candidates_by_thumbnail_id) if seeds else 0
        similar_candidates = 0
        if len(candidates_by_thumbnail_id) < safe_limit and seeds:
            similar_candidates = self._collect_similar_movie_candidates(seeds, candidates_by_thumbnail_id)
        popular_candidates = 0
        if len(candidates_by_thumbnail_id) < safe_limit:
            popular_candidates = self._collect_popular_candidates(candidates_by_thumbnail_id, safe_limit)

        ranked = self._rank_candidates(list(candidates_by_thumbnail_id.values()), safe_limit)
        generated_at = utc_now_for_db()
        rows = [
            {
                "rank": rank,
                "score": candidate.score,
                "strategy": candidate.strategy,
                "reason": candidate.reason,
                "movie": candidate.movie.id,
                "media": candidate.media.id,
                "thumbnail": candidate.thumbnail.id,
                "offset_seconds": candidate.thumbnail.offset,
                "seed_point": candidate.seed_point_id,
                "seed_thumbnail": candidate.seed_thumbnail_id,
                "source_movie": candidate.source_movie_id,
                "visual_score": candidate.visual_score,
                "movie_similarity_score": candidate.movie_similarity_score,
                "generated_at": generated_at,
                "created_at": generated_at,
                "updated_at": generated_at,
            }
            for rank, candidate in enumerate(ranked, start=1)
        ]
        with get_database().atomic():
            # 本次生成成功完成后以新推荐池为准；空候选表示清空旧池，而不是继续展示过期数据。
            MomentRecommendation.delete().execute()
            if rows:
                MomentRecommendation.insert_many(rows).execute()

        stats = {
            "seed_points": len(seeds),
            "visual_candidates": visual_candidates,
            "similar_candidates": similar_candidates,
            "popular_candidates": popular_candidates,
            "stored_items": len(ranked),
        }
        self._emit_progress(progress_callback, **stats)
        return stats

    @classmethod
    def list_items(cls, page: int = 1, page_size: int = 20) -> MomentRecommendationPageResource:
        validate_page(int(page), int(page_size), error_code="invalid_moment_recommendation_filter")
        start = (int(page) - 1) * int(page_size)
        valid_recommendation_query = MomentRecommendation.select().join(Media).where(Media.valid == True)
        total = valid_recommendation_query.count()
        # 分页与 total 都基于仍然有效的媒体，避免失效推荐占用页面槽位。
        generated_row = (
            MomentRecommendation.select(MomentRecommendation.generated_at)
            .join(Media)
            .where(Media.valid == True)
            .order_by(MomentRecommendation.generated_at.desc())
            .first()
        )
        rows = list(
            valid_recommendation_query
            .order_by(MomentRecommendation.rank.asc())
            .offset(start)
            .limit(int(page_size))
        )
        if not rows:
            return MomentRecommendationPageResource(
                items=[],
                page=int(page),
                page_size=int(page_size),
                total=total,
                generated_at=generated_row.generated_at if generated_row is not None else None,
            )

        thumbnail_by_id = cls._get_thumbnails_by_ids([row.thumbnail_id for row in rows])
        movie_query, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies_by_id = {movie.id: movie for movie in movie_query.where(Movie.id.in_([row.movie_id for row in rows]))}
        MovieRecommendationService._attach_movie_flags(list(movies_by_id.values()))

        items: list[MomentRecommendationItemResource] = []
        for row in rows:
            thumbnail = thumbnail_by_id.get(row.thumbnail_id)
            movie = movies_by_id.get(row.movie_id)
            if thumbnail is None or movie is None:
                continue
            base_resource = MovieListItemResource.from_attributes_model(movie)
            base_resource.can_play = bool(getattr(movie, "can_play", False))
            base_resource.is_4k = bool(getattr(movie, "is_4k", False))
            items.append(
                MomentRecommendationItemResource(
                    recommendation_id=row.id,
                    rank=row.rank,
                    score=row.score,
                    strategy=row.strategy,
                    reason=row.reason,
                    media_id=row.media_id,
                    thumbnail_id=row.thumbnail_id,
                    offset_seconds=row.offset_seconds,
                    image=ImageResource.from_attributes_model(thumbnail.image),
                    movie=base_resource,
                )
            )
        return MomentRecommendationPageResource(
            items=items,
            page=int(page),
            page_size=int(page_size),
            total=total,
            generated_at=generated_row.generated_at if generated_row is not None else None,
        )
