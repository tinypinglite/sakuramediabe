"""插件宿主四类基础能力的最小公开契约。"""

from __future__ import annotations

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    Image,
    Media,
    MediaClip,
    MediaLibrary,
    MediaPoint,
    MediaThumbnail,
    Movie,
    PlaylistMovie,
    SystemNotification,
)
from src.plugins import (
    MovieQueryFilters,
    PluginCollection,
    PluginContext,
    PluginNotification,
    PluginSubscription,
)


def _movie(**overrides) -> Movie:
    values = {
        "javdb_id": f"javdb-{overrides.get('movie_number', 'ABP-001')}",
        "movie_number": "ABP-001",
        "title": "测试影片",
        "summary": "简介",
    }
    values.update(overrides)
    return Movie.create(**values)


def test_plugin_movie_query_uses_host_filters_and_cursor(test_db, tmp_path):
    first = _movie(movie_number="ABP-001", title="目标影片", is_subscribed=True)
    _movie(movie_number="ABP-002", title="其他影片", is_subscribed=False)
    third = _movie(movie_number="ABP-003", title="另一个目标", is_subscribed=True)
    library = MediaLibrary.create(
        name="query-library",
        provider_key="local",
        provider_config={},
    )
    Media.create(movie=third, library=library, file_name="ABP-003.mp4")
    api = PluginContext("query_demo", {}, tmp_path).movies

    page = api.query(
        MovieQueryFilters(status="subscribed", search="目标影片"),
        limit=1,
    )

    assert [item.movie_id for item in page.items] == [first.id]
    assert page.next_cursor is None
    assert api.query({"status": "unsubscribed"}).items[0].values["movie_number"] == "ABP-002"
    first_page = api.query({"subscribed": True}, limit=1)
    assert first_page.next_cursor == first.id
    second_page = api.query(
        {"subscribed": True}, after_id=first_page.next_cursor, limit=1
    )
    assert [item.movie_id for item in second_page.items] == [third.id]
    assert [item.movie_id for item in api.query({
        "subscribed": True,
        "playable": True,
    }).items] == [third.id]


def test_plugin_subscriptions_expose_host_status_and_safe_mutations(test_db, tmp_path):
    movie = _movie(movie_number="ABP-010", is_subscribed=False)
    api = PluginContext("subscription_demo", {}, tmp_path).subscriptions

    api.subscribe(movie.movie_number)
    item = api.get(movie.id)

    assert isinstance(item, PluginSubscription)
    assert item.status == "pending"
    assert item.movie_number == movie.movie_number
    assert api.count_by_status().counts["pending"] == 1
    assert api.reset_search([movie.id]) == 1
    assert api.reset_search([]) == 0

    api.unsubscribe(movie.movie_number)
    assert api.get(movie.id) is None


def test_plugin_notifications_are_deduplicated_per_plugin(test_db, tmp_path):
    first_api = PluginContext("notify_one", {}, tmp_path).notifications
    second_api = PluginContext("notify_two", {}, tmp_path).notifications

    first = first_api.create_once(
        category="info",
        title="任务完成",
        content="已完成",
        dedupe_key="job-1",
        event_type="plugin_job",
    )
    repeated = first_api.create_once(
        category="info",
        title="任务完成",
        content="已完成",
        dedupe_key="job-1",
        event_type="plugin_job",
    )
    other = second_api.create_once(
        category="info",
        title="任务完成",
        content="已完成",
        dedupe_key="job-1",
        event_type="plugin_job",
    )

    assert isinstance(first, PluginNotification)
    assert repeated.notification_id == first.notification_id
    assert other.notification_id != first.notification_id
    assert SystemNotification.select().count() == 2
    assert first.dedupe_key == "job-1"
    assert SystemNotification.get_by_id(first.notification_id).dedupe_key == (
        "plugin:notify_one:job-1"
    )
    assert first_api.resolve(first.dedupe_key) == 1


def test_plugin_collections_are_keyed_and_owned(test_db, tmp_path, monkeypatch):
    first = _movie(movie_number="ABP-020")
    second = _movie(movie_number="ABP-021", javdb_id="javdb-ABP-021")
    context = PluginContext("collection_demo", {}, tmp_path)

    playlist = context.collections.ensure_playlist("daily", "每日推荐")
    updated = context.collections.set_playlist_movies(
        "daily", [second.movie_number, first.movie_number, first.movie_number]
    )
    assert isinstance(playlist, PluginCollection)
    assert updated.collection_id == playlist.collection_id
    assert updated.member_count == 2
    assert PlaylistMovie.select().where(
        PlaylistMovie.playlist == playlist.collection_id
    ).count() == 2
    assert context.collections.ensure_playlist("daily", "每日推荐（更新）").collection_id == playlist.collection_id

    library = MediaLibrary.create(
        name="plugin-collection-library",
        provider_key="local",
        provider_config={},
    )
    image = Image.create(
        origin="collection-cover.jpg",
        small="collection-cover-small.jpg",
        medium="collection-cover-medium.jpg",
        large="collection-cover-large.jpg",
    )
    media = Media.create(movie=first, library=library, file_name="ABP-020.mp4")
    thumbnail = MediaThumbnail.create(media=media, image=image, offset=0)
    point = MediaPoint.create(media=media, thumbnail=thumbnail, offset_seconds=1)
    clip = MediaClip.create(
        media=media,
        movie_number=first.movie_number,
        start_offset_seconds=1,
        end_offset_seconds=2,
        file_path="",
        file_size_bytes=4,
        duration_seconds=1,
    )
    clip_root = tmp_path / "media-clips"
    clip_path = clip_root / first.movie_number / f"{clip.id}.mp4"
    clip_path.parent.mkdir(parents=True)
    clip_path.write_bytes(b"clip")
    clip.file_path = f"{first.movie_number}/{clip.id}.mp4"
    clip.save(only=[MediaClip.file_path, MediaClip.file_size_bytes, MediaClip.duration_seconds])
    monkeypatch.setattr("src.config.config.settings.media.media_clip_root_path", str(clip_root))
    invalid_clip = MediaClip.create(
        media=media,
        movie_number=first.movie_number,
        start_offset_seconds=3,
        end_offset_seconds=4,
        file_path="",
    )

    moment = context.collections.ensure_moment("moments", "精选时刻")
    assert context.collections.set_moment_points("moments", [point.id]).member_count == 1
    clips = context.collections.ensure_clip("clips", "精选片段")
    assert context.collections.set_clip_clips(
        "clips", [clip.id, invalid_clip.id]
    ).member_count == 1

    other_context = PluginContext("other_collection_demo", {}, tmp_path)
    with pytest.raises(ApiError) as caught:
        other_context.collections.set_playlist_movies("daily", [first.movie_number])
    assert caught.value.status_code == 404
    assert moment.collection_type == "moment"
    assert clips.collection_type == "clip"
