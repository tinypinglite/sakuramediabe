"""智能列表接口的回归测试：筛选快照、实时成员与只读约束。"""

from datetime import datetime

from src.model import (
    Actor,
    Media,
    MediaLibrary,
    MediaMediaTag,
    MediaProgress,
    MediaTag,
    Movie,
    MovieActor,
    MovieRating,
    MovieTag,
    Playlist,
    PlaylistMovie,
    Tag,
)


def _login(client, account_user) -> dict[str, str]:
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_movie(
    movie_number: str,
    *,
    heat: int = 0,
    is_collection: bool = False,
) -> Movie:
    return Movie.create(
        movie_number=movie_number,
        title=movie_number,
        heat=heat,
        is_collection=is_collection,
    )


def _create_smart_playlist(
    client,
    headers: dict[str, str],
    filter_payload: dict,
    *,
    name: str = "高分片单",
) -> dict:
    response = client.post(
        "/playlists",
        headers=headers,
        json={"name": name, "kind": "smart", "filter": filter_payload},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _numbers(payload: dict) -> set[str]:
    return {item["movie_number"] for item in payload["items"]}


def _link_actors(movie: Movie, *actors: Actor) -> None:
    for actor in actors:
        MovieActor.create(movie=movie, actor=actor)


def test_create_smart_playlist_returns_filter_and_live_count(client, account_user):
    headers = _login(client, account_user)
    _create_movie("SMART-HOT-001", heat=900)
    _create_movie("SMART-COLD-001", heat=10)

    body = _create_smart_playlist(client, headers, {"heat_min": 500})

    assert body["kind"] == "smart"
    assert body["is_smart"] is True
    assert body["smart_filter"]["heat_min"] == 500
    assert body["movie_count"] == 1
    assert body["is_deletable"] is True


def test_smart_playlist_movies_follow_saved_filter(client, account_user):
    headers = _login(client, account_user)
    _create_movie("SMART-SINGLE-001")
    _create_movie("SMART-COLLECTION-001", is_collection=True)
    body = _create_smart_playlist(
        client,
        headers,
        {"collection_type": "single"},
        name="单体片单",
    )

    response = client.get(f"/playlists/{body['id']}/movies", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert _numbers(payload) == {"SMART-SINGLE-001"}
    assert payload["items"][0]["playlist_item_updated_at"] is not None


def test_update_smart_playlist_filter_changes_membership(client, account_user):
    headers = _login(client, account_user)
    _create_movie("SMART-UPDATE-HOT", heat=900)
    _create_movie("SMART-UPDATE-COLD", heat=10)
    body = _create_smart_playlist(client, headers, {"heat_min": 500})

    response = client.patch(
        f"/playlists/{body['id']}",
        headers=headers,
        json={"filter": {"heat_min": 0}},
    )

    assert response.status_code == 200
    assert response.json()["smart_filter"]["heat_min"] == 0
    assert response.json()["movie_count"] == 2


def test_smart_playlist_filter_is_immutable_membership(client, account_user):
    headers = _login(client, account_user)
    movie = _create_movie("SMART-READONLY-001", heat=900)
    body = _create_smart_playlist(client, headers, {"heat_min": 500})

    response = client.put(
        f"/playlists/{body['id']}/movies/{movie.movie_number}",
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "smart_playlist_membership_read_only"


def test_smart_playlist_create_validation(client, account_user):
    headers = _login(client, account_user)

    missing_filter = client.post(
        "/playlists",
        headers=headers,
        json={"name": "缺少筛选", "kind": "smart"},
    )
    assert missing_filter.status_code == 422
    assert missing_filter.json()["error"]["code"] == "smart_playlist_filter_required"

    custom_with_filter = client.post(
        "/playlists",
        headers=headers,
        json={"name": "普通列表", "filter": {"heat_min": 1}},
    )
    assert custom_with_filter.status_code == 422
    assert (
        custom_with_filter.json()["error"]["code"]
        == "smart_playlist_filter_not_supported"
    )

    invalid_resolution = client.post(
        "/playlists",
        headers=headers,
        json={
            "name": "坏分辨率",
            "kind": "smart",
            "filter": {"resolutions": ["4K", "16K"]},
        },
    )
    assert invalid_resolution.status_code == 422
    assert invalid_resolution.json()["error"]["code"] == "invalid_playlist_filter"

    invalid_years = client.post(
        "/playlists",
        headers=headers,
        json={
            "name": "坏年份",
            "kind": "smart",
            "filter": {"years": [99999]},
        },
    )
    assert invalid_years.status_code == 422
    assert invalid_years.json()["error"]["code"] == "validation_error"

    custom = Playlist.create(name="已有普通列表")
    reject_update = client.patch(
        f"/playlists/{custom.id}",
        headers=headers,
        json={"filter": {"heat_min": 1}},
    )
    assert reject_update.status_code == 422
    assert (
        reject_update.json()["error"]["code"]
        == "smart_playlist_filter_not_supported"
    )


def test_smart_playlist_saved_watch_status_and_request_override(client, account_user):
    headers = _login(client, account_user)
    library = MediaLibrary.create(
        name="smart-watch-library",
        provider_key="demo",
        provider_config={},
    )
    finished = _create_movie("SMART-WATCH-FINISHED")
    watching = _create_movie("SMART-WATCH-WATCHING")
    finished_media = Media.create(
        movie=finished,
        library=library,
        file_name="smart-finished.mp4",
        duration_seconds=7200,
    )
    watching_media = Media.create(
        movie=watching,
        library=library,
        file_name="smart-watching.mp4",
        duration_seconds=7200,
    )
    MediaProgress.create(media=finished_media, position_seconds=7000)
    MediaProgress.create(media=watching_media, position_seconds=1800)
    body = _create_smart_playlist(
        client,
        headers,
        {"watch_statuses": ["unwatched"]},
        name="未看片单",
    )

    saved = client.get(f"/playlists/{body['id']}/movies", headers=headers).json()
    assert saved["total"] == 0

    overridden = client.get(
        f"/playlists/{body['id']}/movies",
        headers=headers,
        params={"watch_status": "watched"},
    ).json()
    assert overridden["total"] == 1
    assert _numbers(overridden) == {"SMART-WATCH-FINISHED"}


def test_smart_playlist_actor_filter_supports_any_and_all(client, account_user):
    headers = _login(client, account_user)
    actor_a = Actor.create(name="智能女优A", gender=1)
    actor_b = Actor.create(name="智能女优B", gender=1)
    only_a = _create_movie("SMART-ACTOR-A")
    only_b = _create_movie("SMART-ACTOR-B")
    both = _create_movie("SMART-ACTOR-AB")
    _create_movie("SMART-ACTOR-NONE")
    _link_actors(only_a, actor_a)
    _link_actors(only_b, actor_b)
    _link_actors(both, actor_a, actor_b)

    any_body = _create_smart_playlist(
        client,
        headers,
        {"actor_ids": [actor_a.id, actor_b.id], "actor_match": "or"},
        name="任一女优",
    )
    assert any_body["smart_filter"]["actor_ids"] == [actor_a.id, actor_b.id]
    assert any_body["smart_filter"]["actor_match"] == "or"
    assert any_body["movie_count"] == 3
    any_movies = client.get(
        f"/playlists/{any_body['id']}/movies", headers=headers
    ).json()
    assert _numbers(any_movies) == {
        "SMART-ACTOR-A",
        "SMART-ACTOR-B",
        "SMART-ACTOR-AB",
    }

    all_body = _create_smart_playlist(
        client,
        headers,
        {"actor_ids": [actor_a.id, actor_b.id], "actor_match": "and"},
        name="共演片单",
    )
    assert all_body["movie_count"] == 1
    all_movies = client.get(
        f"/playlists/{all_body['id']}/movies", headers=headers
    ).json()
    assert _numbers(all_movies) == {"SMART-ACTOR-AB"}


def test_smart_playlist_empty_actor_ids_means_unbounded(client, account_user):
    headers = _login(client, account_user)
    _create_movie("SMART-ACTOR-EMPTY-1")
    _create_movie("SMART-ACTOR-EMPTY-2")

    body = _create_smart_playlist(client, headers, {"actor_ids": []}, name="空女优")

    assert body["movie_count"] == 2


def test_smart_playlist_actor_filter_resolves_merged_actor(client, account_user):
    headers = _login(client, account_user)
    canonical = Actor.create(name="规范女优", gender=1)
    merged = Actor.create(name="被合并女优", gender=1, merged_into=canonical)
    movie = _create_movie("SMART-ACTOR-MERGED")
    _link_actors(movie, canonical)

    body = _create_smart_playlist(
        client, headers, {"actor_ids": [merged.id]}, name="合并女优"
    )

    assert body["movie_count"] == 1


def test_smart_playlist_multi_status_supports_any_and_all(client, account_user):
    headers = _login(client, account_user)
    library = MediaLibrary.create(
        name="smart-status-library",
        provider_key="demo",
        provider_config={},
    )
    subscribed = _create_movie("SMART-STATUS-SUB")
    subscribed.is_subscribed = True
    subscribed.save(only=[Movie.is_subscribed])
    _create_movie("SMART-STATUS-UNSUB")
    playable = _create_movie("SMART-STATUS-PLAY")
    playable.is_subscribed = True
    playable.save(only=[Movie.is_subscribed])
    Media.create(
        movie=playable,
        library=library,
        file_name="smart-playable.mp4",
        duration_seconds=7200,
    )

    any_body = _create_smart_playlist(
        client,
        headers,
        {"statuses": ["subscribed", "playable"], "status_match": "or"},
        name="已订阅或可播放",
    )
    assert any_body["smart_filter"]["statuses"] == ["subscribed", "playable"]
    assert any_body["smart_filter"]["status_match"] == "or"
    assert any_body["movie_count"] == 2

    all_body = _create_smart_playlist(
        client,
        headers,
        {"statuses": ["subscribed", "playable"], "status_match": "and"},
        name="已订阅且可播放",
    )
    assert all_body["movie_count"] == 1

    empty_body = _create_smart_playlist(
        client,
        headers,
        {"statuses": ["subscribed", "unsubscribed"], "status_match": "and"},
        name="既是订阅又未订阅",
    )
    assert empty_body["movie_count"] == 0


def test_smart_playlist_multi_watch_status_supports_any_and_all(
    client, account_user
):
    headers = _login(client, account_user)
    library = MediaLibrary.create(
        name="smart-watch-multi-library",
        provider_key="demo",
        provider_config={},
    )
    watching = _create_movie("SMART-WATCH-MULTI-WATCHING")
    watched = _create_movie("SMART-WATCH-MULTI-WATCHED")
    watching_media = Media.create(
        movie=watching,
        library=library,
        file_name="multi-watching.mp4",
        duration_seconds=7200,
    )
    watched_media = Media.create(
        movie=watched,
        library=library,
        file_name="multi-watched.mp4",
        duration_seconds=7200,
    )
    MediaProgress.create(media=watching_media, position_seconds=1800)
    MediaProgress.create(media=watched_media, position_seconds=7000)

    any_body = _create_smart_playlist(
        client,
        headers,
        {"watch_statuses": ["watching", "watched"], "watch_match": "or"},
        name="在看或已看",
    )
    assert any_body["movie_count"] == 2

    all_body = _create_smart_playlist(
        client,
        headers,
        {"watch_statuses": ["watching", "watched"], "watch_match": "and"},
        name="既在看又已看",
    )
    assert all_body["movie_count"] == 0


def test_smart_playlist_multi_resolution_and_year_filters(client, account_user):
    headers = _login(client, account_user)
    library = MediaLibrary.create(
        name="smart-resolution-library",
        provider_key="demo",
        provider_config={},
    )
    four_k = _create_movie("SMART-RES-4K")
    four_k.release_date = datetime(2024, 5, 1)
    four_k.save(only=[Movie.release_date])
    full_hd = _create_movie("SMART-RES-1080P")
    full_hd.release_date = datetime(2023, 5, 1)
    full_hd.save(only=[Movie.release_date])
    Media.create(
        movie=four_k,
        library=library,
        file_name="smart-4k.mp4",
        duration_seconds=7200,
        resolution="3840x2160",
    )
    Media.create(
        movie=full_hd,
        library=library,
        file_name="smart-1080p.mp4",
        duration_seconds=7200,
        resolution="1920x1080",
    )

    resolutions = _create_smart_playlist(
        client,
        headers,
        {"resolutions": ["4K", "1080P"]},
        name="多分辨率",
    )
    assert resolutions["smart_filter"]["resolutions"] == ["4K", "1080P"]
    assert resolutions["movie_count"] == 2

    single_resolution = _create_smart_playlist(
        client,
        headers,
        {"resolutions": ["4K"]},
        name="仅 4K",
    )
    assert single_resolution["movie_count"] == 1

    years = _create_smart_playlist(
        client,
        headers,
        {"years": [2024, 2023]},
        name="多年份",
    )
    assert years["movie_count"] == 2


def test_smart_playlist_lifts_legacy_single_filters(client, account_user):
    headers = _login(client, account_user)

    body = _create_smart_playlist(
        client,
        headers,
        {
            "status": "playable",
            "watch_status": "unwatched",
            "resolution": "4K",
        },
        name="旧格式片单",
    )

    smart_filter = body["smart_filter"]
    assert smart_filter["statuses"] == ["playable"]
    assert smart_filter["watch_statuses"] == ["unwatched"]
    assert smart_filter["resolutions"] == ["4K"]


def test_smart_playlist_temporary_filters_override_and_clear(
    client, account_user
):
    headers = _login(client, account_user)
    library = MediaLibrary.create(
        name="smart-temporary-filter-library",
        provider_key="demo",
        provider_config={},
    )
    movie_tag_a = Tag.create(name="临时筛选A")
    movie_tag_b = Tag.create(name="临时筛选B")
    media_tag = MediaTag.create(name="临时媒体标签", include_terms=[], exclude_terms=[])

    first = Movie.create(
        movie_number="SMART-TEMP-001",
        title="SMART-TEMP-001",
        release_date=datetime(2024, 5, 1),
    )
    second = Movie.create(
        movie_number="SMART-TEMP-002",
        title="SMART-TEMP-002",
        release_date=datetime(2023, 5, 1),
    )
    third = Movie.create(
        movie_number="SMART-TEMP-003",
        title="SMART-TEMP-003",
        release_date=datetime(2024, 6, 1),
        is_subscribed=True,
    )
    MovieTag.create(movie=first, tag=movie_tag_a)
    MovieTag.create(movie=second, tag=movie_tag_b)
    MovieTag.create(movie=third, tag=movie_tag_a)

    first_media = Media.create(
        movie=first,
        library=library,
        file_name="smart-temp-001.mp4",
        duration_seconds=7200,
        resolution="3840x2160",
    )
    second_media = Media.create(
        movie=second,
        library=library,
        file_name="smart-temp-002.mp4",
        duration_seconds=7200,
        resolution="1920x1080",
    )
    MediaMediaTag.create(media=first_media, tag=media_tag)
    MediaProgress.create(media=second_media, position_seconds=7000)

    body = _create_smart_playlist(
        client,
        headers,
        {
            "statuses": ["playable"],
            "watch_statuses": ["unwatched"],
            "years": [2024],
            "resolutions": ["4K"],
            "tag_ids": [movie_tag_a.id],
            "media_tag_ids": [media_tag.id],
        },
        name="临时筛选测试",
    )
    endpoint = f"/playlists/{body['id']}/movies"

    saved = client.get(endpoint, headers=headers)
    assert saved.status_code == 200
    assert _numbers(saved.json()) == {"SMART-TEMP-001"}

    overridden = client.get(
        endpoint,
        headers=headers,
        params={
            "statuses": "playable",
            "watch_statuses": "watched",
            "year": "2023",
            "resolutions": "1080P",
            "tag_ids": str(movie_tag_b.id),
            "media_tag_ids": "",
        },
    )
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["total"] == 1
    assert _numbers(overridden.json()) == {"SMART-TEMP-002"}

    cleared = client.get(
        endpoint,
        headers=headers,
        params={
            "statuses": "",
            "watch_statuses": "",
            "year": "",
            "resolutions": "",
            "tag_ids": "",
            "media_tag_ids": "",
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["total"] == 3
    assert _numbers(cleared.json()) == {
        "SMART-TEMP-001",
        "SMART-TEMP-002",
        "SMART-TEMP-003",
    }


def test_custom_playlist_supports_temporary_filters(client, account_user):
    headers = _login(client, account_user)
    library = MediaLibrary.create(
        name="custom-temporary-filter-library",
        provider_key="demo",
        provider_config={},
    )
    movie_tag = Tag.create(name="自建临时标签")

    first = Movie.create(
        movie_number="CUSTOM-TEMP-001",
        title="CUSTOM-TEMP-001",
        release_date=datetime(2024, 5, 1),
    )
    second = Movie.create(
        movie_number="CUSTOM-TEMP-002",
        title="CUSTOM-TEMP-002",
        release_date=datetime(2023, 5, 1),
    )
    MovieTag.create(movie=second, tag=movie_tag)
    Media.create(
        movie=first,
        library=library,
        file_name="custom-temp-001.mp4",
        duration_seconds=7200,
        resolution="3840x2160",
    )
    second_media = Media.create(
        movie=second,
        library=library,
        file_name="custom-temp-002.mp4",
        duration_seconds=7200,
        resolution="1920x1080",
    )
    MediaProgress.create(media=second_media, position_seconds=7000)
    playlist = Playlist.create(name="普通临时筛选", kind="custom")
    PlaylistMovie.create(playlist=playlist, movie=first)
    PlaylistMovie.create(playlist=playlist, movie=second)

    response = client.get(
        f"/playlists/{playlist.id}/movies",
        headers=headers,
        params={
            "statuses": "playable",
            "watch_statuses": "watched",
            "year": "2023",
            "resolutions": "1080P",
            "tag_ids": str(movie_tag.id),
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1
    assert _numbers(response.json()) == {"CUSTOM-TEMP-002"}


def test_playlist_sort_supports_unified_fields_for_custom_and_smart(
    client, account_user
):
    headers = _login(client, account_user)
    library = MediaLibrary.create(
        name="playlist-unified-sort-library",
        provider_key="demo",
        provider_config={},
    )
    older = Movie.create(
        movie_number="SORT-001",
        title="SORT-001",
        release_date=datetime(2022, 1, 1),
        subscribed_at=datetime(2025, 1, 1),
    )
    newer = Movie.create(
        movie_number="SORT-002",
        title="SORT-002",
        release_date=datetime(2023, 1, 1),
        subscribed_at=datetime(2026, 1, 1),
    )
    unrated = Movie.create(
        movie_number="SORT-003",
        title="SORT-003",
        release_date=datetime(2021, 1, 1),
    )
    Media.create(
        movie=older,
        library=library,
        file_name="sort-001.mp4",
        duration_seconds=7200,
        video_info={"video": {"bit_rate": 1000}},
    )
    Media.create(
        movie=newer,
        library=library,
        file_name="sort-002.mp4",
        duration_seconds=7200,
        video_info={"video": {"bit_rate": 3000}},
    )
    MovieRating.create(movie=older, values={}, total_score=3.0)
    MovieRating.create(movie=newer, values={}, total_score=5.0)

    custom = Playlist.create(name="统一排序普通列表", kind="custom")
    for movie in (older, newer, unrated):
        PlaylistMovie.create(playlist=custom, movie=movie)
    smart = _create_smart_playlist(
        client,
        headers,
        {"collection_type": "all"},
        name="统一排序智能列表",
    )

    expected = {
        "bitrate:desc": ["SORT-002", "SORT-001", "SORT-003"],
        "my_score:desc": ["SORT-002", "SORT-001", "SORT-003"],
        "subscribed_at:desc": ["SORT-002", "SORT-001", "SORT-003"],
    }
    for path in (
        f"/playlists/{custom.id}/movies",
        f"/playlists/{smart['id']}/movies",
    ):
        for sort, numbers in expected.items():
            response = client.get(path, headers=headers, params={"sort": sort})
            assert response.status_code == 200, response.text
            assert [item["movie_number"] for item in response.json()["items"]] == numbers


def test_playlist_temporary_filter_rejects_invalid_values(client, account_user):
    headers = _login(client, account_user)
    body = _create_smart_playlist(
        client,
        headers,
        {"collection_type": "all"},
        name="非法临时筛选",
    )
    endpoint = f"/playlists/{body['id']}/movies"

    for params in (
        {"statuses": "invalid"},
        {"watch_statuses": "invalid"},
        {"tag_ids": "0"},
        {"year": "2024,invalid"},
        {"resolutions": "5K"},
    ):
        response = client.get(endpoint, headers=headers, params=params)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_playlist_filter"
