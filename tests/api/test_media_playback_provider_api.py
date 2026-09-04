import hashlib
import hmac
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.responses import PlainTextResponse

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)
from tests.conftest import TEST_FILE_SIGNATURE_EXPIRES, TEST_FILE_SIGNATURE_SECRET


def _media(test_name: str):
    library = MediaLibrary.create(name=f"{test_name}-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number=f"{test_name}-001", javdb_id=f"{test_name}-1", title=test_name)
    return Media.create(movie=movie, library=library, file_name="media.mp4")


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _merged_url(media_ids: tuple[int, ...], resource_path: str = "stream.mp4") -> str:
    signature_payload = (
        f"merged-media:{','.join(str(media_id) for media_id in media_ids)}:"
        f"{resource_path}:{TEST_FILE_SIGNATURE_EXPIRES}"
    )
    signature = hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        signature_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return (
        f"/media/merged-play/{resource_path}?media_ids="
        f"{','.join(str(media_id) for media_id in media_ids)}"
        f"&expires={TEST_FILE_SIGNATURE_EXPIRES}&signature={signature}"
    )


@pytest.mark.parametrize(
    ("playback_deliveries", "expected_delivery"),
    (
        (("proxy", "redirect"), "proxy"),
        (("redirect", "proxy"), "redirect"),
        (("proxy",), "proxy"),
    ),
)
def test_media_playback_gateway_uses_provider_default_delivery(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
    playback_deliveries,
    expected_delivery,
):
    media = _media("gateway")
    seen = {}

    class Storage:
        async def handle_playback(self, *, media, context):
            seen["media_id"] = media.media_id
            seen["resource_path"] = context.resource_path
            seen["delivery"] = context.delivery
            seen["child_url"] = context.url_for("hls/next.ts")
            return PlainTextResponse("provider response")

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=playback_deliveries),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    response = client.get(
        build_signed_media_url(media.id, "hls/segment.ts").replace("&delivery=proxy", "")
    )

    assert response.status_code == 200
    assert response.text == "provider response"
    assert seen == {
        "media_id": media.id,
        "resource_path": "hls/segment.ts",
        "delivery": expected_delivery,
        "child_url": build_signed_media_url(
            media.id, "hls/next.ts", delivery=expected_delivery
        ),
    }


def test_media_playback_gateway_records_actual_playback_mode(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
    account_user,
):
    media = _media("gateway-mode")

    class Storage:
        async def handle_playback(self, *, media, context):
            return PlainTextResponse(context.delivery)

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("redirect", "proxy")),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    attempt_id = uuid4().hex

    response = client.get(
        f"{build_signed_media_url(media.id, delivery='redirect')}&playback_attempt_id={attempt_id}"
    )

    assert response.status_code == 200
    mode_response = client.get(
        f"/media/playback-attempts/{attempt_id}",
        headers=_auth_headers(client, account_user),
    )
    assert mode_response.status_code == 200
    assert mode_response.json() == {"mode": "direct"}


def test_media_playback_gateway_maps_provider_error(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
):
    media = _media("gateway-error")

    deliveries = []

    class Storage:
        async def handle_playback(self, *, media, context):
            deliveries.append(context.delivery)
            raise ProviderOperationError(
                provider_key="demo",
                operation="playback",
                code="source_not_found",
                safe_message="source missing",
                retryable=False,
            )

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("redirect", "proxy")),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    response = client.get(build_signed_media_url(media.id, delivery="redirect"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "provider_source_not_found"
    assert deliveries == ["redirect"]


@pytest.mark.parametrize(
    ("code", "retryable"),
    (("unsupported", False), ("unavailable", True)),
)
def test_media_playback_gateway_does_not_change_delivery_after_failure(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
    code,
    retryable,
    account_user,
):
    media = _media("gateway-fallback")
    deliveries = []

    class Storage:
        async def handle_playback(self, *, media, context):
            deliveries.append(context.delivery)
            if context.delivery == "redirect":
                raise ProviderOperationError(
                    provider_key="demo",
                    operation="playback",
                    code=code,
                    safe_message="redirect unavailable",
                    retryable=retryable,
                )
            return PlainTextResponse("proxy response")

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("redirect", "proxy")
        ),
    )
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage()
    )

    attempt_id = uuid4().hex
    response = client.get(
        f"{build_signed_media_url(media.id, delivery='redirect')}&playback_attempt_id={attempt_id}"
    )

    assert response.status_code == (422 if code == "unsupported" else 503)
    assert response.json()["error"]["code"] == f"provider_{code}"
    assert deliveries == ["redirect"]


def test_media_playback_mode_returns_null_for_unknown_attempt(
    client,
    test_db,
    account_user,
):
    response = client.get(
        f"/media/playback-attempts/{uuid4().hex}",
        headers=_auth_headers(client, account_user),
    )

    assert response.status_code == 200
    assert response.json() == {"mode": None}


def test_media_playback_gateway_rejects_provider_unsupported_delivery(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
):
    media = _media("gateway-delivery")
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy",)),
    )
    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "storage_for",
        lambda _handle: (_ for _ in ()).throw(AssertionError("storage must not be built")),
    )

    response = client.get(build_signed_media_url(media.id, delivery="redirect"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "provider_playback_delivery_unsupported"


def test_merged_media_playback_gateway_uses_ordered_group_and_proxy(
    client,
    test_db,
    monkeypatch,
):
    first = _media("merged-gateway")
    second = Media.create(
        movie=first.movie,
        library=first.library,
        file_name="media-cd2.mp4",
    )
    seen = {}

    class Storage:
        async def handle_merged_playback(self, *, medias, context):
            seen["media_ids"] = [media.media_id for media in medias]
            seen["resource_path"] = context.resource_path
            seen["delivery"] = context.delivery
            seen["child_url"] = context.url_for("next.ts")
            return PlainTextResponse("merged provider response")

    monkeypatch.setattr(
        MEDIA_PROVIDER_REGISTRY,
        "require",
        lambda _provider_key: SimpleNamespace(
            playback_deliveries=("proxy",), merged_playback_format="mp4"
        ),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())

    response = client.get(_merged_url((first.id, second.id)))

    assert response.status_code == 200
    assert response.text == "merged provider response"
    assert seen["media_ids"] == [first.id, second.id]
    assert seen["resource_path"] == "stream.mp4"
    assert seen["delivery"] == "proxy"
    assert f"media_ids={first.id},{second.id}" in seen["child_url"]


def test_merged_media_playback_gateway_rejects_cross_library_group(
    client,
    test_db,
):
    first = _media("merged-cross-library")
    other_library = MediaLibrary.create(
        name="other-library", provider_key="demo", provider_config={}
    )
    second = Media.create(
        movie=first.movie,
        library=other_library,
        file_name="media-cd2.mp4",
    )

    response = client.get(_merged_url((first.id, second.id)))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "merged_playback_cross_library"
