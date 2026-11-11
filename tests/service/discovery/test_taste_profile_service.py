from src.model import (
    PLAYLIST_KIND_CUSTOM,
    PLAYLIST_KIND_FAVORITES,
    PLAYLIST_KIND_RECENTLY_PLAYED,
    Actor,
    Image,
    MediaPoint,
    MomentCollection,
    MomentCollectionItem,
    Movie,
    MovieActor,
    MovieTag,
    Playlist,
    PlaylistMovie,
    Tag,
)
from src.service.discovery.taste_profile_service import TasteProfileService


def _create_movie(movie_number: str, **kwargs) -> Movie:
    return Movie.create(
        movie_number=movie_number,
        title=movie_number,
        **kwargs,
    )


def _create_actor(
    name: str,
    *,
    gender: int = 1,
    cup: str | None = "C",
    is_subscribed: bool = False,
    merged_into: Actor | None = None,
) -> Actor:
    return Actor.create(
        name=name,
        gender=gender,
        cup=cup,
        is_subscribed=is_subscribed,
        merged_into=merged_into,
    )


def _link(movie: Movie, *actors: Actor) -> None:
    for actor in actors:
        MovieActor.create(movie=movie, actor=actor)


def _tag(movie: Movie, name: str) -> None:
    tag, _ = Tag.get_or_create(name=name)
    MovieTag.create(movie=movie, tag=tag)


def test_taste_profile_weights_signals_and_filters_technical_tags(test_db):
    recent = Playlist.create(kind=PLAYLIST_KIND_RECENTLY_PLAYED, name="最近播放")
    custom = Playlist.create(kind=PLAYLIST_KIND_CUSTOM, name="自定义")
    plugin_playlist = Playlist.create(
        kind=PLAYLIST_KIND_CUSTOM, name="插件片单", owner_plugin_id="plugin"
    )
    favorites = Playlist.create(kind=PLAYLIST_KIND_FAVORITES, name="收藏")

    lead = _create_actor("lead", cup="C", is_subscribed=True)
    unsubscribed = _create_actor("unsubscribed", cup="B")
    male = _create_actor("male", gender=2, cup="A")
    no_cup = _create_actor("no-cup", cup=None, is_subscribed=True)
    merged = _create_actor("merged", cup="D", merged_into=lead)
    one_hit = _create_actor("one-hit", cup="E")

    play_and_custom = _create_movie("PLAY-CUSTOM")
    favorite_movie = _create_movie("FAVORITE")
    moment_movie = _create_movie("MOMENT")
    custom_only = _create_movie("CUSTOM-ONLY")
    blacklisted = _create_movie("BLACKLISTED", is_blacklisted=True)
    collection = _create_movie("COLLECTION", is_collection=True)
    plugin_movie = _create_movie("PLUGIN")

    PlaylistMovie.create(playlist=recent, movie=play_and_custom)
    PlaylistMovie.create(playlist=custom, movie=play_and_custom)
    PlaylistMovie.create(playlist=favorites, movie=favorite_movie)
    PlaylistMovie.create(playlist=custom, movie=custom_only)
    PlaylistMovie.create(playlist=recent, movie=blacklisted)
    PlaylistMovie.create(playlist=recent, movie=collection)
    PlaylistMovie.create(playlist=plugin_playlist, movie=plugin_movie)

    image = Image.create(origin="test/moment.jpg")
    moment_collection = MomentCollection.create(name="时刻")
    point = MediaPoint.create(
        image=image, movie_number=moment_movie.movie_number, offset_seconds=10
    )
    MomentCollectionItem.create(
        collection=moment_collection, point=point, position=0
    )

    _link(play_and_custom, lead, male, no_cup, one_hit)
    _link(favorite_movie, lead, no_cup)
    _link(custom_only, unsubscribed, merged)
    _link(moment_movie, unsubscribed)
    _link(blacklisted, lead)
    _link(collection, lead)
    _link(plugin_movie, lead)

    _tag(play_and_custom, "单体作品")
    _tag(favorite_movie, "单体作品")
    _tag(play_and_custom, "4K")
    _tag(play_and_custom, "只出现一次")
    _tag(blacklisted, "被屏蔽的标签")

    profile = TasteProfileService.get_profile()

    # 播放 + 自定义片单 + 收藏 + 时刻，排除黑名单和合集后共 4 部。
    assert profile.sample_movie_count == 4
    assert [item.actor.name for item in profile.actresses] == [
        "lead",
        "no-cup",
        "unsubscribed",
    ]
    assert profile.actresses[0].matched_movie_count == 2
    assert profile.actresses[0].score == 6.0  # 播放 1 + 自定义 2 + 收藏 3
    assert profile.actresses[1].matched_movie_count == 2
    assert profile.actresses[1].score == 6.0  # 没有 cup 资料也不影响进入结果
    assert profile.actresses[2].matched_movie_count == 2
    assert profile.actresses[2].score == 3.0  # 自定义 2 + 时刻 1

    # 订阅女优不在第二个列表里；男优、已合并和只命中一次的女优不出现。
    assert [item.actor.name for item in profile.unsubscribed_actresses] == [
        "unsubscribed"
    ]

    # 技术标签被名单排除，只命中一次的标签不进入结果。
    assert [item.name for item in profile.tags] == ["单体作品"]
    assert profile.tags[0].matched_movie_count == 2
    assert profile.tags[0].score == 6.0


def test_taste_profile_returns_empty_lists_without_signals(test_db):
    profile = TasteProfileService.get_profile()

    assert profile.sample_movie_count == 0
    assert profile.actresses == []
    assert profile.unsubscribed_actresses == []
    assert profile.tags == []
