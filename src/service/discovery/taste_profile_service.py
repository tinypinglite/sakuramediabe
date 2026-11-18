from __future__ import annotations

from collections import defaultdict

from peewee import JOIN

from src.common.runtime_time import utc_now_for_db
from src.model import (
    PLAYLIST_KIND_CUSTOM,
    PLAYLIST_KIND_FAVORITES,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Actor,
    MediaPoint,
    MomentCollectionItem,
    Movie,
    MovieActor,
    MovieTag,
    Playlist,
    PlaylistMovie,
    Tag,
)
from src.schema.catalog.actors import ActorResource
from src.schema.discovery import (
    TasteProfileActressResource,
    TasteProfileResource,
    TasteProfileTagResource,
)
from src.service.catalog.actor_service import ActorService


class TasteProfileService:
    """按播放、片单、收藏与时刻加权推导用户的女优和标签偏好。

    现有数据只保存最后状态，没有播放次数或完播率，因此这里是可解释的加权计数，
    不做协同过滤或相似度推荐。
    """

    FAVORITE_WEIGHT = 3.0
    CUSTOM_PLAYLIST_WEIGHT = 2.0
    RECENTLY_PLAYED_WEIGHT = 1.0
    MOMENT_WEIGHT = 1.0
    MIN_MATCHED_MOVIE_COUNT = 2
    ACTRESS_LIMIT = 20
    UNSUBSCRIBED_ACTRESS_LIMIT = 20
    TAG_LIMIT = 20

    # shortcut: 技术属性标签用固定名单排除，出现新变体时补名字；需要用户自定义时再迁到配置。
    # 不能按全库频率过滤：巨乳、中出、单体作品同样高频，但属于用户口味。
    TASTE_TAG_EXCLUDE_NAMES = frozenset(
        {
            "4K",
            "VR",
            "高清",
            "HDTV",
            "数位马赛克",
            "薄马赛克",
            "ハイビジョン(HD)",
            "高品质VR",
            "HD高画質ムービー",
            "ストリーミング配信",
            "独占配信",
            "60fps",
        }
    )

    @classmethod
    def get_profile(cls) -> TasteProfileResource:
        movie_weights = cls._movie_weights()
        actresses, unsubscribed_actresses = cls._actress_resources(movie_weights)
        return TasteProfileResource(
            sample_movie_count=len(movie_weights),
            generated_at=utc_now_for_db(),
            actresses=actresses,
            unsubscribed_actresses=unsubscribed_actresses,
            tags=cls._tag_resources(movie_weights),
        )

    @classmethod
    def _movie_weights(cls) -> dict[int, float]:
        weights: dict[int, float] = defaultdict(float)
        rows = (
            PlaylistMovie.select(
                PlaylistMovie.movie, Playlist.kind, Playlist.owner_plugin_id
            )
            .join(Playlist, JOIN.INNER, on=(PlaylistMovie.playlist == Playlist.id))
            .switch(PlaylistMovie)
            .join(Movie, JOIN.INNER, on=(PlaylistMovie.movie == Movie.id))
            .where(
                Playlist.kind.in_(
                    [
                        PLAYLIST_KIND_RECENTLY_PLAYED,
                        PLAYLIST_KIND_CUSTOM,
                        PLAYLIST_KIND_FAVORITES,
                    ]
                ),
                Movie.is_blacklisted == False,
                Movie.is_collection == False,
            )
            .tuples()
        )
        for movie_id, kind, owner_plugin_id in rows:
            if kind == PLAYLIST_KIND_CUSTOM:
                if owner_plugin_id is not None:
                    continue
                weight = cls.CUSTOM_PLAYLIST_WEIGHT
            elif kind == PLAYLIST_KIND_FAVORITES:
                weight = cls.FAVORITE_WEIGHT
            else:
                weight = cls.RECENTLY_PLAYED_WEIGHT
            weights[movie_id] += weight
        cls._add_moment_weights(weights)
        return dict(weights)

    @classmethod
    def _add_moment_weights(cls, weights: dict[int, float]) -> None:
        rows = (
            Movie.select(Movie.id)
            .join(
                MediaPoint,
                JOIN.INNER,
                on=(Movie.movie_number == MediaPoint.movie_number),
            )
            .join(
                MomentCollectionItem,
                JOIN.INNER,
                on=(MomentCollectionItem.point == MediaPoint.id),
            )
            .where(
                Movie.is_blacklisted == False,
                Movie.is_collection == False,
            )
            .distinct()
            .tuples()
        )
        for (movie_id,) in rows:
            weights[movie_id] += cls.MOMENT_WEIGHT

    @classmethod
    def _actress_resources(
        cls, movie_weights: dict[int, float]
    ) -> tuple[list[TasteProfileActressResource], list[TasteProfileActressResource]]:
        ranked, subscriptions = cls._ranked_actresses(movie_weights)
        ranked = [
            item for item in ranked if item[1] >= cls.MIN_MATCHED_MOVIE_COUNT
        ]
        if not ranked:
            return [], []

        top = ranked[: cls.ACTRESS_LIMIT]
        unsubscribed = [
            item for item in ranked if not subscriptions.get(item[0], False)
        ][: cls.UNSUBSCRIBED_ACTRESS_LIMIT]
        actor_ids = list(
            dict.fromkeys([item[0] for item in top] + [item[0] for item in unsubscribed])
        )
        actors_by_id = {
            actor.id: actor
            for actor in ActorService.actor_query().where(Actor.id.in_(actor_ids))
        }

        def build(items):
            return [
                TasteProfileActressResource(
                    actor=ActorResource.from_actor(actors_by_id[actor_id]),
                    matched_movie_count=count,
                    score=round(score, 1),
                )
                for actor_id, count, score in items
                if actor_id in actors_by_id
            ]

        return build(top), build(unsubscribed)

    @classmethod
    def _ranked_actresses(
        cls, movie_weights: dict[int, float]
    ) -> tuple[list[tuple[int, int, float]], dict[int, bool]]:
        if not movie_weights:
            return [], []
        stats: dict[int, list[float]] = defaultdict(lambda: [0, 0.0])
        subscriptions: dict[int, bool] = {}
        rows = (
            MovieActor.select(
                MovieActor.movie, MovieActor.actor, Actor.is_subscribed
            )
            .join(Actor, JOIN.INNER, on=(MovieActor.actor == Actor.id))
            .where(
                MovieActor.movie.in_(list(movie_weights)),
                Actor.merged_into.is_null(),
                # 不依赖 cup：cup 属于资料补全插件的目标字段，未补全时不应影响女优识别。
                Actor.gender == ActorService.FEMALE_GENDER,
            )
            .tuples()
        )
        for movie_id, actor_id, is_subscribed in rows:
            stats[actor_id][0] += 1
            stats[actor_id][1] += movie_weights[movie_id]
            subscriptions[actor_id] = bool(is_subscribed)
        ranked = sorted(
            (
                (actor_id, int(count), score)
                for actor_id, (count, score) in stats.items()
            ),
            key=lambda item: (-item[2], -item[1], item[0]),
        )
        return ranked, subscriptions

    @classmethod
    def _tag_resources(
        cls, movie_weights: dict[int, float]
    ) -> list[TasteProfileTagResource]:
        if not movie_weights:
            return []
        stats: dict[int, list[float]] = defaultdict(lambda: [0, 0.0])
        names: dict[int, str] = {}
        rows = (
            MovieTag.select(MovieTag.movie, MovieTag.tag, Tag.name)
            .join(Tag, JOIN.INNER, on=(MovieTag.tag == Tag.id))
            .where(
                MovieTag.movie.in_(list(movie_weights)),
                Tag.name.not_in(list(cls.TASTE_TAG_EXCLUDE_NAMES)),
            )
            .tuples()
        )
        for movie_id, tag_id, name in rows:
            stats[tag_id][0] += 1
            stats[tag_id][1] += movie_weights[movie_id]
            names[tag_id] = name
        ranked = sorted(
            (
                (tag_id, int(count), score)
                for tag_id, (count, score) in stats.items()
                if count >= cls.MIN_MATCHED_MOVIE_COUNT
            ),
            key=lambda item: (-item[2], -item[1], item[0]),
        )
        return [
            TasteProfileTagResource(
                tag_id=tag_id,
                name=names[tag_id],
                matched_movie_count=count,
                score=round(score, 1),
            )
            for tag_id, count, score in ranked[: cls.TAG_LIMIT]
        ]
