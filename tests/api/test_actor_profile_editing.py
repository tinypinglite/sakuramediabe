from io import BytesIO

from PIL import Image as PillowImage

from src.config.config import settings
from src.metadata._providers.models import JavdbMovieActorResource
from src.model import Actor, Image
from src.service.catalog.catalog_import_service import CatalogImportService


def _auth_headers(client, account_user):
    response = client.post(
        "/auth/tokens",
        json={"username": account_user.username, "password": "password123"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _png_bytes() -> bytes:
    output = BytesIO()
    PillowImage.new("RGB", (12, 8), (120, 30, 30)).save(output, format="PNG")
    return output.getvalue()


def _gif_bytes() -> bytes:
    output = BytesIO()
    PillowImage.new("P", (12, 8), 1).save(output, format="GIF")
    return output.getvalue()


def test_actor_display_name_falls_back_to_source_name(client, account_user):
    actor = Actor.create(
        javdb_id="actor-display-name-fallback",
        name="来源名称",
        alias_name="来源别名",
    )
    headers = _auth_headers(client, account_user)

    response = client.get(f"/actors/{actor.id}", headers=headers)

    assert response.status_code == 200
    assert response.json()["display_name"] == "来源名称"


def test_patch_actor_profile_preserves_source_identity_and_marks_manual_fields(
    client, account_user
):
    actor = Actor.create(
        javdb_id="actor-local-profile",
        name="来源名称",
        alias_name="来源别名",
        gender=1,
    )
    headers = _auth_headers(client, account_user)

    response = client.patch(
        f"/actors/{actor.id}",
        headers=headers,
        json={
            "display_name_override": "我的显示名",
            "birthday": "1998-04-12",
            "height_cm": 160,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["display_name"] == "我的显示名"
    assert payload["name"] == "来源名称"
    assert payload["gender"] == 1
    assert payload["birthday"] == "1998-04-12"
    assert payload["height_cm"] == 160
    assert payload["mutation_revision"] == 1
    assert payload["manual_fields"] == ["birthday", "height_cm"]

    CatalogImportService().upsert_actor_from_javdb_resource(
        JavdbMovieActorResource(
            javdb_id=actor.javdb_id,
            name="来源新名称",
            alias_names=["来源新别名"],
        )
    )
    refreshed = client.get(f"/actors/{actor.id}", headers=headers)

    assert refreshed.status_code == 200
    assert refreshed.json()["name"] == "来源新名称"
    assert refreshed.json()["display_name"] == "我的显示名"
    assert refreshed.json()["gender"] == 1


def test_patch_actor_profile_uses_last_write_wins_for_local_display_name(
    client, account_user
):
    actor = Actor.create(javdb_id="actor-revision", name="演员")
    headers = _auth_headers(client, account_user)

    first = client.patch(
        f"/actors/{actor.id}",
        headers=headers,
        json={"display_name_override": "第一次"},
    )
    second = client.patch(
        f"/actors/{actor.id}",
        headers=headers,
        json={"display_name_override": "第二次"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["display_name"] == "第二次"
    assert second.json()["mutation_revision"] == 0


def test_actor_profile_image_upload_and_clear_restore_source_image(
    client, account_user, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "assets"))
    source_image = Image.create(
        origin="actors/source.webp",
        small="actors/source.webp",
        medium="actors/source.webp",
        large="actors/source.webp",
    )
    actor = Actor.create(
        javdb_id="actor-avatar",
        name="演员",
        profile_image=source_image,
    )
    headers = _auth_headers(client, account_user)

    upload = client.put(
        f"/actors/{actor.id}/profile-image",
        headers=headers,
        files={"file": ("avatar.png", _png_bytes(), "image/png")},
    )

    assert upload.status_code == 200
    uploaded = upload.json()
    assert uploaded["has_profile_image_override"] is True
    assert uploaded["profile_image"]["origin"].startswith("/files/images/actors/manual/")
    override = Actor.get_by_id(actor.id).profile_image_override
    assert override is not None
    assert (tmp_path / "assets" / override.origin).is_file()

    cleared = client.delete(
        f"/actors/{actor.id}/profile-image",
        headers=headers,
    )

    assert cleared.status_code == 200
    assert cleared.json()["has_profile_image_override"] is False
    assert cleared.json()["profile_image"]["id"] == source_image.id
    assert Image.get_or_none(Image.id == override.id) is None
    assert not (tmp_path / "assets" / override.origin).exists()


def test_actor_profile_image_rejects_gif(client, account_user):
    actor = Actor.create(javdb_id="actor-gif", name="演员")
    headers = _auth_headers(client, account_user)

    response = client.put(
        f"/actors/{actor.id}/profile-image",
        headers=headers,
        files={"file": ("avatar.gif", _gif_bytes(), "image/gif")},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_actor_profile_image"
