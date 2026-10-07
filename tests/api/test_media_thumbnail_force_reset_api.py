import zipfile
from types import SimpleNamespace

from src.config.config import settings
from src.model import Image, Media, MediaLibrary, MediaPoint, MediaThumbnail, Movie
from src.service.playback import media_service
from src.service.playback.thumbnails.artifacts import ThumbnailArtifactService
from src.service.playback.thumbnails.task_service import MediaThumbnailTaskService


def _auth_headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/auth/tokens", json={"username": username, "password": "password123"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _prepare_media_with_pack(
    monkeypatch, tmp_path, *, movie_number: str = "FORCE-001", offsets=(10, 20)
):
    image_root = tmp_path / "assets"
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))
    monkeypatch.setattr(
        media_service,
        "get_qdrant_thumbnail_store",
        lambda: SimpleNamespace(delete_by_media_id=lambda _media_id: None),
    )
    library = MediaLibrary.create(
        name="force-reset-library", provider_key="demo", provider_config={}
    )
    movie = Movie.create(movie_number=movie_number, title="force")
    media = Media.create(
        movie=movie,
        library=library,
        file_name="force.mp4",
        thumbnail_generation_state=Media.THUMBNAIL_STATE_SUCCEEDED,
    )
    thumbnails_dir = ThumbnailArtifactService.thumbnail_directory(media)
    thumbnails_dir.mkdir(parents=True)
    pack_path = ThumbnailArtifactService.thumbnail_pack_file(media)
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_STORED) as archive:
        for offset in offsets:
            archive.writestr(f"{offset}.webp", f"thumb-{offset}".encode())
    thumbnails = []
    for offset in offsets:
        relative_path = (
            (thumbnails_dir / f"{offset}.webp").relative_to(image_root).as_posix()
        )
        image = Image.create(origin=relative_path)
        thumbnails.append(MediaThumbnail.create(media=media, image=image, offset=offset))
    return media, thumbnails, pack_path


def test_force_reset_deletes_existing_thumbnails(
    client, account_user, monkeypatch, tmp_path
):
    media, thumbnails, pack_path = _prepare_media_with_pack(monkeypatch, tmp_path)
    image_ids = [thumbnail.image_id for thumbnail in thumbnails]

    response = client.post(
        "/media/thumbnail-generation/reset",
        json={"media_ids": [media.id], "force": True},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"reset_count": 1}
    reset_media = Media.get_by_id(media.id)
    assert reset_media.thumbnail_generation_state == Media.THUMBNAIL_STATE_PENDING
    assert reset_media.thumbnail_attempt_count == 0
    assert not MediaThumbnail.select().where(MediaThumbnail.media == media).exists()
    assert not Image.select().where(Image.id.in_(image_ids)).exists()
    assert not pack_path.exists()
    # 重置后重新进入生成候选，等待定时任务重生成。
    assert MediaThumbnailTaskService.count_pending_media() == 1


def test_force_reset_keeps_moment_image(client, account_user, monkeypatch, tmp_path):
    media, thumbnails, pack_path = _prepare_media_with_pack(monkeypatch, tmp_path)
    headers = _auth_headers(client, account_user.username)
    created = client.post(
        f"/media/{media.id}/points",
        json={"thumbnail_id": thumbnails[0].id},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    point_id = created.json()["point_id"]
    point_image = MediaPoint.get_by_id(point_id).image
    point_file = tmp_path / "assets" / point_image.origin
    assert point_image.origin.startswith("media_points/")
    assert point_file.read_bytes() == b"thumb-10"

    response = client.post(
        "/media/thumbnail-generation/reset",
        json={"media_ids": [media.id], "force": True},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"reset_count": 1}
    assert MediaPoint.get_or_none(MediaPoint.id == point_id) is not None
    assert Image.get_or_none(Image.id == point_image.id) is not None
    assert point_file.is_file()
    assert not MediaThumbnail.select().where(MediaThumbnail.media == media).exists()
    assert not pack_path.exists()


def test_force_reset_skips_invalid_and_missing_media(
    client, account_user, monkeypatch, tmp_path
):
    media, thumbnails, pack_path = _prepare_media_with_pack(monkeypatch, tmp_path)
    media.valid = False
    media.save()

    response = client.post(
        "/media/thumbnail-generation/reset",
        json={"media_ids": [media.id, 999999], "force": True},
        headers=_auth_headers(client, account_user.username),
    )

    assert response.status_code == 200
    assert response.json() == {"reset_count": 0}
    assert (
        MediaThumbnail.select().where(MediaThumbnail.media == media).count()
        == len(thumbnails)
    )
    assert pack_path.exists()
    assert (
        Media.get_by_id(media.id).thumbnail_generation_state
        == Media.THUMBNAIL_STATE_SUCCEEDED
    )
