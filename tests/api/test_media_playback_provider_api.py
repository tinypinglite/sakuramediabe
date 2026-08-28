from types import SimpleNamespace

from starlette.responses import PlainTextResponse

from src.model import Media, MediaLibrary, Movie
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
)


def _media(test_name: str):
    library = MediaLibrary.create(name=f"{test_name}-library", provider_key="demo", provider_config={})
    movie = Movie.create(movie_number=f"{test_name}-001", javdb_id=f"{test_name}-1", title=test_name)
    return Media.create(movie=movie, library=library, file_name="media.mp4")


def test_media_playback_gateway_passes_opaque_path_to_provider(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
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
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy", "redirect")),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    response = client.get(build_signed_media_url(media.id, "hls/segment.ts"))

    assert response.status_code == 200
    assert response.text == "provider response"
    assert seen == {
        "media_id": media.id,
        "resource_path": "hls/segment.ts",
        "delivery": "proxy",
        "child_url": build_signed_media_url(media.id, "hls/next.ts"),
    }


def test_media_playback_gateway_maps_provider_error(
    client,
    test_db,
    build_signed_media_url,
    monkeypatch,
):
    media = _media("gateway-error")

    class Storage:
        async def handle_playback(self, *, media, context):
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
        lambda _provider_key: SimpleNamespace(playback_deliveries=("proxy", "redirect")),
    )
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: Storage())
    response = client.get(build_signed_media_url(media.id))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "provider_source_not_found"


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
