from types import SimpleNamespace

from src.model import (
    Media,
    MediaLibrary,
    MediaMediaTag,
    MediaTag,
    Movie,
    RankingItem,
)
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from src.service.discovery import ranking_service
from src.service.discovery.ranking_service import (
    RankingBoardDefinition,
    RankingSourceDefinition,
)


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_movie_detail_exposes_media_playback_deliveries(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(name="detail-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="DETAIL-001", title="detail")
    media = Media.create(movie=movie, library=library, file_name="detail.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy", "redirect")),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert response.json()["media_count"] == 1
    assert response.json()["media_items"][0]["library_name"] == "detail-library"
    assert response.json()["media_items"][0]["media_id"] == media.id
    assert response.json()["media_items"][0]["playback_deliveries"] == ["proxy", "redirect"]


def test_movie_detail_exposes_media_tags_and_source_snapshot(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="detail-tag-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(movie_number="DETAIL-TAG-001", title="detail")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="DETAIL-TAG-001.mp4",
        release_title="DETAIL-TAG-001 1080p",
        release_description="source description",
        source_indexer="indexer-a",
        info_hash="0123456789abcdef0123456789abcdef01234567",
        download_task_id=77,
    )
    tag = MediaTag.create(name="detail-tag", include_terms=["detail"], exclude_terms=[])
    MediaMediaTag.create(media=media, tag=tag)
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy",)),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    media_item = response.json()["media_items"][0]
    assert media_item["tags"] == [{"id": tag.id, "name": "detail-tag"}]
    assert media_item["is_media_tags_manually_managed"] is False
    assert media_item["release_title"] == "DETAIL-TAG-001 1080p"
    assert media_item["release_description"] == "source description"
    assert media_item["source_indexer"] == "indexer-a"
    assert media_item["info_hash"] == "0123456789abcdef0123456789abcdef01234567"
    assert media_item["download_task_id"] == 77


def test_movie_detail_exposes_ranking_placements(client, account_user, monkeypatch):
    movie = Movie.create(movie_number="RANK-001", title="rank")
    for period, rank in (("weekly", 12), ("daily", 3)):
        RankingItem.create(
            source_key="javdb",
            board_key="playback_all",
            period=period,
            rank=rank,
            movie=movie,
            movie_number=movie.movie_number,
        )
    RankingItem.create(
        source_key="retired",
        board_key="legacy",
        period="",
        rank=1,
        movie=movie,
        movie_number=movie.movie_number,
    )
    monkeypatch.setitem(
        ranking_service.RANKING_SOURCES,
        "javdb",
        RankingSourceDefinition(
            key="javdb",
            name="JavDB",
            boards=(RankingBoardDefinition(key="playback_all", name="热播"),),
        ),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert response.json()["rankings"] == [
        {
            "source_key": "javdb",
            "source_name": "JavDB",
            "board_key": "playback_all",
            "board_name": "热播",
            "period": "daily",
            "rank": 3,
        },
        {
            "source_key": "javdb",
            "source_name": "JavDB",
            "board_key": "playback_all",
            "board_name": "热播",
            "period": "weekly",
            "rank": 12,
        },
    ]


def test_movie_detail_exposes_merge_playback_candidate_for_supported_library(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(name="merge-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number="MERGE-001", title="merge")
    first = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    second = Media.create(movie=movie, library=library, file_name="merge-cd2.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert [item["media_id"] for item in response.json()["media_items"]] == [first.id, second.id]
    assert response.json()["merge_playback_candidates"] == [
        {
            "library_id": library.id,
            "library_name": "merge-library",
            "provider_key": "demo",
            "segment_count": 2,
        }
    ]


def test_merged_playback_url_uses_ordered_same_library_media(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="merge-url-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number="MERGE-URL-001", title="merge")
    first = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    second = Media.create(movie=movie, library=library, file_name="merge-cd2.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )
    class Storage:
        pass

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())

    response = client.get(
        f"/movies/{movie.movie_number}/merged-playback?library_id={library.id}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert (
        f"/media/merged-play/stream.mp4?media_ids={first.id},{second.id}"
        in response.json()["play_url"]
    )


def test_merged_playback_url_uses_selected_ordered_media_ids(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="merge-selected-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number="MERGE-SELECTED-001", title="merge")
    first = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    second = Media.create(movie=movie, library=library, file_name="merge-cd2.mp4")
    third = Media.create(movie=movie, library=library, file_name="merge-cd3.mp4")
    seen: list[list[int]] = []
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )

    class Storage:
        @staticmethod
        def preflight_merged_playback(*, medias):
            seen.append([media.media_id for media in medias])

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())

    response = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={library.id}&media_ids={third.id},{first.id}"
        ),
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert seen == [[third.id, first.id]]
    assert (
        f"/media/merged-play/stream.mp4?media_ids={third.id},{first.id}"
        in response.json()["play_url"]
    )
    assert f"media_ids={first.id},{second.id}" not in response.json()["play_url"]


def test_merged_playback_selected_media_must_share_library(
    client,
    account_user,
    monkeypatch,
):
    first_library = MediaLibrary.create(
        name="merge-selected-lib-a", provider_key="demo", provider_config={}
    )
    second_library = MediaLibrary.create(
        name="merge-selected-lib-b", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number="MERGE-SELECTED-CROSS-001", title="merge")
    first = Media.create(movie=movie, library=first_library, file_name="merge-cd1.mp4")
    second = Media.create(movie=movie, library=second_library, file_name="merge-cd2.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )

    response = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={first_library.id}&media_ids={first.id},{second.id}"
        ),
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "merged_playback_cross_library"


def test_merged_playback_selected_media_requires_two_unique_ids(
    client,
    account_user,
):
    library = MediaLibrary.create(
        name="merge-selected-validation-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(movie_number="MERGE-SELECTED-VALIDATION-001", title="merge")
    media = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    headers = _auth_headers(client, account_user)

    too_few = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={library.id}&media_ids={media.id}"
        ),
        headers=headers,
    )
    duplicated = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={library.id}&media_ids={media.id},{media.id}"
        ),
        headers=headers,
    )

    assert too_few.status_code == 422
    assert too_few.json()["error"]["code"] == "merged_playback_need_at_least_two"
    assert duplicated.status_code == 422
    assert duplicated.json()["error"]["code"] == "invalid_merged_playback"


def test_merged_playback_selected_media_rejects_invalid_or_foreign_ids(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="merge-selected-invalid-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(movie_number="MERGE-SELECTED-INVALID-001", title="merge")
    valid = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    invalid = Media.create(
        movie=movie,
        library=library,
        file_name="merge-cd2.mp4",
        valid=False,
    )
    other_movie = Movie.create(movie_number="MERGE-SELECTED-OTHER-001", title="other")
    foreign = Media.create(
        movie=other_movie,
        library=library,
        file_name="other-cd1.mp4",
    )
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )
    headers = _auth_headers(client, account_user)

    invalid_response = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={library.id}&media_ids={valid.id},{invalid.id}"
        ),
        headers=headers,
    )
    foreign_response = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={library.id}&media_ids={valid.id},{foreign.id}"
        ),
        headers=headers,
    )
    missing_response = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={library.id}&media_ids={valid.id},999999"
        ),
        headers=headers,
    )

    assert invalid_response.status_code == 422
    assert invalid_response.json()["error"]["code"] == "merged_playback_unavailable"
    assert foreign_response.status_code == 404
    assert foreign_response.json()["error"]["code"] == "media_not_found"
    assert missing_response.status_code == 404
    assert missing_response.json()["error"]["code"] == "media_not_found"


def test_merged_playback_selected_media_rejects_unsupported_provider(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="merge-selected-unsupported-library",
        provider_key="demo",
        provider_config={},
    )
    movie = Movie.create(movie_number="MERGE-SELECTED-UNSUPPORTED-001", title="merge")
    first = Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    second = Media.create(movie=movie, library=library, file_name="merge-cd2.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy",)),
    )

    response = client.get(
        (
            f"/movies/{movie.movie_number}/merged-playback"
            f"?library_id={library.id}&media_ids={first.id},{second.id}"
        ),
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "merged_playback_unavailable"


def test_merged_playback_url_rejects_provider_preflight_failure(
    client,
    account_user,
    monkeypatch,
):
    library = MediaLibrary.create(
        name="merge-preflight-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(
        movie_number="MERGE-PREFLIGHT-001", title="merge"
    )
    Media.create(movie=movie, library=library, file_name="merge-cd1.mp4")
    Media.create(movie=movie, library=library, file_name="merge-cd2.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )

    class Storage:
        @staticmethod
        def preflight_merged_playback(*, medias):
            raise ProviderOperationError(
                provider_key="demo",
                operation="merged_playback",
                code="unsupported",
                safe_message="分段视频规格不一致（编码/分辨率/帧率不同），不支持合并播放",
                retryable=False,
            )

    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())

    response = client.get(
        f"/movies/{movie.movie_number}/merged-playback?library_id={library.id}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_unsupported"


def test_movie_detail_omits_play_url_for_invalid_media(client, account_user, monkeypatch):
    library = MediaLibrary.create(
        name="detail-validity-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(
        movie_number="DETAIL-VALID-001", title="validity"
    )
    invalid_media = Media.create(
        movie=movie, library=library, file_name="invalid.mp4", valid=False
    )
    valid_media = Media.create(movie=movie, library=library, file_name="valid.mp4")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy",)),
    )

    response = client.get(
        f"/movies/{movie.movie_number}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["can_play"] is True
    media_by_id = {item["media_id"]: item for item in body["media_items"]}
    assert media_by_id[invalid_media.id]["play_url"] == ""
    assert media_by_id[valid_media.id]["play_url"] != ""
