"""基于 Qdrant 稀疏向量的影片相似度索引与查询。"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from typing import Iterator, Sequence

from loguru import logger
from peewee import fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import (
    find_movie_by_number,
    parse_special_tags_text,
    with_movie_card_relations,
)
from src.model import Media, Movie, MovieActor, MovieTag
from src.schema.catalog.movies import MovieListItemResource
from src.service.discovery.qdrant_movie_similarity_store import (
    MovieSimilarityIndexError,
    MovieSimilarityIndexNotReadyError,
    MovieSimilaritySearchHit,
    MovieSimilarityUnavailableError,
    QdrantMovieSimilarityStore,
    get_qdrant_movie_similarity_store,
)


SIM_WEIGHT_ACTOR = 0.6
SIM_WEIGHT_TAG = 0.4
SIM_TOP_N = 50
INDEX_BATCH_SIZE = 1000
# 每轮取多少部影片的特征；分段读取避免全量结果集常驻内存，也不需要长事务。
FEATURE_PAGE_SIZE = 2000


class SimilarMovieItem:
    """承载相似影片的中间结果，供 schema 层组装响应。"""

    def __init__(self, movie: Movie, can_play: bool, similarity_score: float) -> None:
        self.movie = movie
        self.can_play = can_play
        self.similarity_score = similarity_score


class MovieRecommendationService:
    """影片元数据稀疏索引的构建与相似影片查询。"""

    def __init__(
        self,
        *,
        store: QdrantMovieSimilarityStore | None = None,
    ) -> None:
        self.store = store or get_qdrant_movie_similarity_store()

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    @staticmethod
    def _load_feature_document_frequencies(
        link_model,
        feature_field,
    ) -> dict[int, int]:
        rows = (
            link_model.select(feature_field, fn.COUNT(link_model.movie))
            .join(Movie, on=(link_model.movie == Movie.id))
            .where(Movie.is_collection == False)
            .group_by(feature_field)
            .tuples()
        )
        return {int(feature_id): int(count) for feature_id, count in rows}

    @staticmethod
    def _load_feature_groups(
        link_model,
        feature_field,
        movie_ids: Sequence[int],
    ) -> dict[int, list[int]]:
        """取出指定影片分段内的全部特征，按影片聚合。"""
        groups: dict[int, list[int]] = defaultdict(list)
        rows = (
            link_model.select(link_model.movie, feature_field)
            .where(link_model.movie.in_(list(movie_ids)))
            .tuples()
        )
        for movie_id, feature_id in rows:
            groups[int(movie_id)].append(int(feature_id))
        return groups

    @classmethod
    def _iter_movie_features(
        cls,
    ) -> Iterator[tuple[int, list[int], list[int]]]:
        """按影片 id 分段读取特征；段内一次取全，无需长事务与服务端游标。"""
        last_movie_id = 0
        while True:
            movie_ids = [
                int(movie_id)
                for (movie_id,) in Movie.select(Movie.id)
                .where(
                    Movie.is_collection == False,
                    Movie.id > last_movie_id,
                )
                .order_by(Movie.id)
                .limit(FEATURE_PAGE_SIZE)
                .tuples()
            ]
            if not movie_ids:
                return
            last_movie_id = movie_ids[-1]

            actor_groups = cls._load_feature_groups(
                MovieActor, MovieActor.actor, movie_ids
            )
            tag_groups = cls._load_feature_groups(MovieTag, MovieTag.tag, movie_ids)
            for movie_id in movie_ids:
                actor_ids = actor_groups.get(movie_id, [])
                tag_ids = tag_groups.get(movie_id, [])
                # 没有任何演员/标签的影片构造不出向量，直接跳过不入索引。
                if not actor_ids and not tag_ids:
                    continue
                yield movie_id, actor_ids, tag_ids

            # 不满一页说明已经读到末尾，省掉一次必然为空的探测查询。
            if len(movie_ids) < FEATURE_PAGE_SIZE:
                return

    @staticmethod
    def _build_sparse_vector(
        actor_ids: Sequence[int],
        tag_ids: Sequence[int],
        *,
        actor_df: dict[int, int],
        tag_df: dict[int, int],
        total_movies: int,
    ) -> tuple[list[int], list[float]]:
        # 文档频次先于特征扫描加载，重建期间新入库的演员/标签取 DF=0（IDF 拉满）。
        actor_idfs = [
            math.log((total_movies + 1) / (actor_df.get(actor_id, 0) + 1)) + 1.0
            for actor_id in actor_ids
        ]
        tag_idfs = [
            math.log((total_movies + 1) / (tag_df.get(tag_id, 0) + 1)) + 1.0
            for tag_id in tag_ids
        ]
        actor_norm = math.sqrt(sum(value * value for value in actor_idfs))
        tag_norm = math.sqrt(sum(value * value for value in tag_idfs))
        weighted_features = []
        if actor_norm > 0:
            actor_scale = math.sqrt(SIM_WEIGHT_ACTOR) / actor_norm
            weighted_features.extend(
                (actor_id * 2, idf * actor_scale)
                for actor_id, idf in zip(actor_ids, actor_idfs)
            )
        if tag_norm > 0:
            tag_scale = math.sqrt(SIM_WEIGHT_TAG) / tag_norm
            weighted_features.extend(
                (tag_id * 2 + 1, idf * tag_scale)
                for tag_id, idf in zip(tag_ids, tag_idfs)
            )
        weighted_features.sort(key=lambda item: item[0])
        return (
            [index for index, _ in weighted_features],
            [float(value) for _, value in weighted_features],
        )

    def _purge_orphan_collections(self) -> int:
        """清理历史遗留集合：alias 切换失败或进程中断都会留下未被引用的集合。"""
        active_collection = self.store.get_alias_target()
        purged_count = 0
        for collection_name in self.store.list_index_collections():
            if collection_name == active_collection:
                continue
            try:
                self.store.delete_collection(collection_name)
                purged_count += 1
            except MovieSimilarityIndexError as exc:
                logger.warning(
                    "清理遗留影片相似度索引集合失败 collection={} detail={}",
                    collection_name,
                    exc,
                )
        if purged_count:
            logger.warning(
                "清理遗留影片相似度索引集合 purged_collections={}", purged_count
            )
        return purged_count

    def recompute_all(
        self,
        *,
        progress_callback=None,
    ) -> dict[str, int]:
        """流式重建完整索引，成功后原子切换查询 alias。"""
        total_movies = int(
            Movie.select().where(Movie.is_collection == False).count()
        )
        actor_df = self._load_feature_document_frequencies(
            MovieActor, MovieActor.actor
        )
        tag_df = self._load_feature_document_frequencies(MovieTag, MovieTag.tag)
        stats = {
            "total_movies": total_movies,
            "indexed_movies": 0,
            "actor_features": sum(actor_df.values()),
            "tag_features": sum(tag_df.values()),
        }
        self._emit_progress(
            progress_callback,
            current=0,
            total=total_movies,
            text="开始构建影片相似度索引",
            summary_patch=stats,
        )

        self._purge_orphan_collections()
        collection_name = f"{self.store.COLLECTION_PREFIX}{time.time_ns()}"
        self.store.create_collection(collection_name)
        batch: list[tuple[int, list[int], list[float]]] = []

        def _flush_batch() -> None:
            """写入整批后再累加计数并上报，保证 stats 与索引内实际点数同步推进。"""
            if not batch:
                return
            self.store.upsert_sparse_points(collection_name, batch)
            stats["indexed_movies"] += len(batch)
            batch.clear()
            self._emit_progress(
                progress_callback,
                current=stats["indexed_movies"],
                total=total_movies,
                text=f"已索引 {stats['indexed_movies']}/{total_movies}",
                summary_patch=stats,
            )

        try:
            for movie_id, actor_ids, tag_ids in self._iter_movie_features():
                indices, values = self._build_sparse_vector(
                    actor_ids,
                    tag_ids,
                    actor_df=actor_df,
                    tag_df=tag_df,
                    total_movies=total_movies,
                )
                batch.append((movie_id, indices, values))
                if len(batch) >= INDEX_BATCH_SIZE:
                    _flush_batch()
            _flush_batch()

            stored_count = self.store.count(collection_name)
            if stored_count != stats["indexed_movies"]:
                raise RuntimeError(
                    "影片相似度索引点数校验失败 "
                    f"expected={stats['indexed_movies']} actual={stored_count}"
                )
        except Exception:
            try:
                self.store.delete_collection(collection_name)
            except MovieSimilarityIndexError as cleanup_exc:
                logger.warning(
                    "清理失败的影片相似度索引集合失败 collection={} detail={}",
                    collection_name,
                    cleanup_exc,
                )
            raise

        # alias 切换结果存在网络层歧义，切换报错时不能删除可能已激活的新集合。
        old_collection = self.store.activate_collection(collection_name)
        if old_collection and old_collection != collection_name:
            try:
                self.store.delete_collection(old_collection)
            except MovieSimilarityIndexError as exc:
                logger.warning(
                    "清理旧影片相似度索引集合失败 collection={} detail={}",
                    old_collection,
                    exc,
                )

        self._emit_progress(
            progress_callback,
            current=total_movies,
            total=total_movies,
            text="影片相似度索引构建完成",
            summary_patch=stats,
        )
        logger.info(
            "movie similarity index rebuilt total_movies={} indexed_movies={} "
            "actor_features={} tag_features={}",
            stats["total_movies"],
            stats["indexed_movies"],
            stats["actor_features"],
            stats["tag_features"],
        )
        return stats

    def search_similar_movies(
        self,
        source_movie_ids: Sequence[int],
        *,
        limit: int = SIM_TOP_N,
    ) -> dict[int, list[MovieSimilaritySearchHit]]:
        return self.store.search_many(source_movie_ids, limit=limit)

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
            movie.can_play = movie.movie_number in playable_movie_numbers
            movie.is_4k = movie.movie_number in is_4k_movie_numbers

    def list_similar(
        self,
        movie_number: str,
        limit: int = 20,
    ) -> list[SimilarMovieItem]:
        source_movie = find_movie_by_number(movie_number)
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
        try:
            hits = self.search_similar_movies(
                [source_movie.id],
                limit=safe_limit,
            )[source_movie.id]
        except MovieSimilarityIndexNotReadyError as exc:
            raise ApiError(
                503,
                "movie_similarity_index_not_ready",
                "影片相似度索引尚未完成首次构建",
            ) from exc
        except MovieSimilarityUnavailableError as exc:
            # 与每日/瞬时推荐保持一致：Qdrant 故障只降级相似度信号，不让详情页整体报错。
            logger.warning(
                "相似影片查询跳过：影片相似度服务不可用 movie_number={} detail={}",
                movie_number,
                exc,
            )
            return []
        if not hits:
            return []

        target_ids = [hit.movie_id for hit in hits]
        score_by_target_id = {hit.movie_id: hit.score for hit in hits}
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
                    similarity_score=score_by_target_id[target_id],
                )
            )
        return items

    def list_similar_resources(
        self,
        movie_number: str,
        limit: int = 20,
    ):
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
