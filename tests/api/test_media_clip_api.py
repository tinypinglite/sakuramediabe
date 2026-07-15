import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.config.config import settings
from src.model import Image, Media, MediaClip, MediaLibrary, MediaThumbnail, Movie
from src.lib.cloud115.types import DirectUrl
from src.service.playback.media_clip_service import MediaClipService
from tests.conftest import TEST_FILE_SIGNATURE_EXPIRES, TEST_FILE_SIGNATURE_SECRET


def _login(client, username="account", password="password123"):
    response = client.post("/auth/tokens", json={"username": username, "password": password})
    return response.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _create_media(tmp_path, movie_number="ABC-001") -> Media:
    movie = Movie.create(movie_number=movie_number, javdb_id=f"jav-{movie_number}", title=movie_number)
    source = tmp_path / f"{movie_number}.mp4"
    source.write_bytes(b"video-bytes")
    media = Media.create(movie=movie, path=str(source), valid=True)
    for offset in (0, 10, 20, 30):
        path = f"movies/{movie_number}/media/fp/thumbnails/{offset}.webp"
        image = Image.create(origin=path, small=path, medium=path, large=path)
        MediaThumbnail.create(media=media, image=image, offset=offset)
    return media


def _create_cloud_media(movie_number="ABC-115") -> Media:
    movie = Movie.create(movie_number=movie_number, javdb_id=f"jav-{movie_number}", title=movie_number)
    library = MediaLibrary.create(
        name=f"Cloud-{movie_number}",
        backend="cloud115",
        backend_account_key=f"cloud115:{movie_number}",
        backend_config={"cookies": f"UID={movie_number}_A1_x", "root_cid": "root", "app": "web"},
    )
    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={
            "fid": f"fid-{movie_number}",
            "pickcode": f"pc-{movie_number}",
            "name": f"{movie_number}.mp4",
            "source_path": f"incoming/{movie_number}.mp4",
        },
        content_fingerprint=f"sha1:{movie_number}",
        valid=True,
    )
    for offset in (0, 10, 20, 30):
        path = f"movies/{movie_number}/media/fp/thumbnails/{offset}.webp"
        image = Image.create(origin=path, small=path, medium=path, large=path)
        MediaThumbnail.create(media=media, image=image, offset=offset)
    return media


def _thumb_id(media, offset):
    return MediaThumbnail.get(MediaThumbnail.media == media, MediaThumbnail.offset == offset).id


@pytest.fixture()
def clip_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.media, "media_clip_root_path", str(tmp_path / "clips"))
    monkeypatch.setattr(
        MediaClipService,
        "_cut_clip_file",
        staticmethod(lambda source, target, start, end: target.write_bytes(b"clip-bytes")),
    )


def _build_signed_clip_url(clip_id, expires=TEST_FILE_SIGNATURE_EXPIRES):
    payload = f"clip:{clip_id}:{expires}"
    signature = hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"/media-clips/{clip_id}/stream?expires={expires}&signature={signature}"


def test_clip_endpoints_require_authentication(client):
    assert client.post("/media/1/clips", json={"start_thumbnail_id": 1, "end_thumbnail_id": 2}).status_code == 401
    assert client.get("/media/1/clips").status_code == 401
    assert client.get("/media-clips").status_code == 401
    assert client.get("/media-clips/1").status_code == 401
    assert client.get("/media-clips/1/thumbnails").status_code == 401
    assert client.patch("/media-clips/1", json={"title": "x"}).status_code == 401
    assert client.delete("/media-clips/1").status_code == 401


def test_create_clip_returns_201_then_200_on_duplicate(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    body = {"start_thumbnail_id": _thumb_id(media, 10), "end_thumbnail_id": _thumb_id(media, 30)}

    first = client.post(f"/media/{media.id}/clips", json=body, headers=_auth(token))
    second = client.post(f"/media/{media.id}/clips", json=body, headers=_auth(token))

    assert first.status_code == 201
    payload = first.json()
    assert payload["start_offset_seconds"] == 10
    assert payload["end_offset_seconds"] == 30
    assert payload["cover_image"] is not None
    assert payload["stream_url"].startswith("/media-clips/")
    assert second.status_code == 200
    assert second.json()["clip_id"] == payload["clip_id"]


def test_create_cloud115_clip_uses_cloud_source(
    client, account_user, clip_storage, monkeypatch
):
    token = _login(client, username=account_user.username)
    media = _create_cloud_media()
    calls = []

    def fake_cut(cls, received_media, target, start, end):
        calls.append((received_media.id, start, end))
        target.write_bytes(b"cloud-clip-bytes")

    monkeypatch.setattr(
        MediaClipService,
        "_cut_cloud115_clip_file",
        classmethod(fake_cut),
    )

    response = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 10), "end_thumbnail_id": _thumb_id(media, 30)},
        headers=_auth(token),
    )

    assert response.status_code == 201
    assert calls == [(media.id, 10, 30)]
    assert response.json()["file_size_bytes"] == len(b"cloud-clip-bytes")


def test_cut_cloud115_clip_resolves_direct_url_with_bound_user_agent(
    monkeypatch, tmp_path
):
    import src.lib.cloud115 as cloud115_module
    from src.service.playback import cloud115_backend_service as backend_module

    calls = []
    reader_calls = []

    class FakeClient:
        async def get_download_url(self, pickcode, user_agent):
            calls.append((pickcode, user_agent))
            return DirectUrl(
                file_id="fid",
                file_name="movie.mp4",
                # 某些 downurl 响应不带大小；应回退到 Media 已登记的大小。
                file_size=0,
                sha1="ABC",
                pickcode=pickcode,
                url="https://cdn.example.com/movie.mp4?t=9999999999",
                user_agent=user_agent,
                expires_at=9999999999,
            )

    @asynccontextmanager
    async def fake_client_for(_library):
        yield FakeClient()

    class FakeRangeReader:
        def __init__(self, url, *, user_agent, file_size, max_fetched_bytes=None):
            # 记录 max_fetched_bytes 以断言切片路径始终传了抓取预算，防止 CDN
            # 不遵守 Range 时把整个大文件读进内存。
            reader_calls.append((url, user_agent, file_size, max_fetched_bytes))
            self.closed = False

        def close(self):
            self.closed = True

    remux_calls = []
    monkeypatch.setattr(backend_module, "cloud115_client_for", fake_client_for)
    monkeypatch.setattr(cloud115_module, "Cloud115RangeReader", FakeRangeReader)
    monkeypatch.setattr(
        MediaClipService,
        "_remux_cloud115_clip",
        staticmethod(lambda reader, target, start, end: remux_calls.append(
            (reader, target, start, end)
        )),
    )
    media = SimpleNamespace(
        id=1,
        library=object(),
        backend_locator={"pickcode": "pc-clip"},
        file_size_bytes=2048,
    )
    target = tmp_path / "clip.mp4"

    MediaClipService._cut_cloud115_clip_file(media, target, 10, 30)

    assert calls == [("pc-clip", MediaClipService.CLOUD115_CLIP_USER_AGENT)]
    assert reader_calls == [(
        "https://cdn.example.com/movie.mp4?t=9999999999",
        MediaClipService.CLOUD115_CLIP_USER_AGENT,
        2048,
        MediaClipService.CLOUD115_CLIP_FETCH_BUDGET_BYTES,
    )]
    assert len(remux_calls) == 1
    reader, received_target, start, end = remux_calls[0]
    assert (received_target, start, end) == (target, 10, 30)
    assert reader.closed is True


def test_run_ffmpeg_clip_builds_local_input_command(monkeypatch, tmp_path):
    commands = []
    target = tmp_path / "clip.mp4"

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        target.write_bytes(b"clip")

    monkeypatch.setattr("src.service.playback.media_clip_service.subprocess.run", fake_run)

    MediaClipService._run_ffmpeg_clip(
        "/library/movie.mp4",
        target,
        5,
        15,
    )

    command, kwargs = commands[0]
    assert "-user_agent" not in command
    assert command[command.index("-i") + 1] == "/library/movie.mp4"
    assert kwargs["check"] is True


def test_remux_cloud115_clip_from_seekable_reader(tmp_path):
    import av

    source = tmp_path / "source.mp4"
    target = tmp_path / "target.mp4"
    with av.open(str(source), mode="w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width = 64
        stream.height = 48
        stream.pix_fmt = "yuv420p"
        for index in range(30):
            frame = av.VideoFrame(64, 48, "yuv420p")
            frame.pts = index
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)

    with source.open("rb") as reader:
        MediaClipService._remux_cloud115_clip(reader, target, 1, 2)

    assert target.stat().st_size > 0
    with av.open(str(target)) as container:
        assert container.streams.video
        assert container.duration is not None and container.duration > 0


def test_create_clip_rejects_invalid_range(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    body = {"start_thumbnail_id": _thumb_id(media, 10), "end_thumbnail_id": _thumb_id(media, 10)}

    response = client.post(f"/media/{media.id}/clips", json=body, headers=_auth(token))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_clip_invalid_range"


def test_list_and_detail_clip(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    created = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 20)},
        headers=_auth(token),
    ).json()

    per_media = client.get(f"/media/{media.id}/clips", headers=_auth(token))
    global_list = client.get("/media-clips", headers=_auth(token))
    detail = client.get(f"/media-clips/{created['clip_id']}", headers=_auth(token))

    assert per_media.status_code == 200
    assert len(per_media.json()) == 1
    assert global_list.status_code == 200
    assert global_list.json()["total"] == 1
    assert detail.status_code == 200
    # 区间 [0, 20] 内缩略图为 0/10/20 三帧。
    assert len(detail.json()["preview_frames"]) == 3
    # 未加入任何合集时 collections 为空数组（前端选择器据此回显勾选）。
    assert detail.json()["collections"] == []


def test_list_media_clips_filters_by_movie_number(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media_a = _create_media(tmp_path, movie_number="ABC-001")
    media_b = _create_media(tmp_path, movie_number="XYZ-999")
    client.post(
        f"/media/{media_a.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media_a, 0), "end_thumbnail_id": _thumb_id(media_a, 10)},
        headers=_auth(token),
    )
    client.post(
        f"/media/{media_b.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media_b, 0), "end_thumbnail_id": _thumb_id(media_b, 10)},
        headers=_auth(token),
    )

    filtered = client.get("/media-clips", params={"movie_number": "ABC-001"}, headers=_auth(token))

    assert filtered.status_code == 200
    body = filtered.json()
    assert body["total"] == 1
    assert [item["movie_number"] for item in body["items"]] == ["ABC-001"]


def test_list_clip_thumbnails_returns_rebased_offsets(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    clip_id = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 10), "end_thumbnail_id": _thumb_id(media, 30)},
        headers=_auth(token),
    ).json()["clip_id"]

    response = client.get(f"/media-clips/{clip_id}/thumbnails", headers=_auth(token))

    assert response.status_code == 200
    body = response.json()
    # 区间 [10,30] 内缩略图 10/20/30，重定基为片段相对 0/10/20。
    assert [item["offset_seconds"] for item in body] == [0, 10, 20]
    assert all(item["clip_id"] == clip_id for item in body)
    # 图像走签名 URL。
    assert body[0]["image"]["origin"].startswith("/files/images/")


def test_update_and_delete_clip(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    clip_id = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    ).json()["clip_id"]

    patched = client.patch(f"/media-clips/{clip_id}", json={"title": "片段A"}, headers=_auth(token))
    assert patched.status_code == 200
    assert patched.json()["title"] == "片段A"

    deleted = client.delete(f"/media-clips/{clip_id}", headers=_auth(token))
    assert deleted.status_code == 204
    assert MediaClip.get_or_none(MediaClip.id == clip_id) is None


def test_stream_clip_with_signature(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    clip_id = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    ).json()["clip_id"]

    full = client.get(_build_signed_clip_url(clip_id))
    partial = client.get(_build_signed_clip_url(clip_id), headers={"Range": "bytes=0-3"})

    assert full.status_code == 200
    assert full.content == b"clip-bytes"
    assert full.headers["accept-ranges"] == "bytes"
    assert partial.status_code == 206
    assert partial.content == b"clip"


def test_stream_clip_rejects_invalid_signature(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    clip_id = client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    ).json()["clip_id"]

    response = client.get(f"/media-clips/{clip_id}/stream?expires=1&signature=bad")

    assert response.status_code == 403


def test_media_clips_path_does_not_collide_with_media_detail(client, account_user, tmp_path, clip_storage):
    token = _login(client, username=account_user.username)
    media = _create_media(tmp_path)
    client.post(
        f"/media/{media.id}/clips",
        json={"start_thumbnail_id": _thumb_id(media, 0), "end_thumbnail_id": _thumb_id(media, 10)},
        headers=_auth(token),
    )

    # /media-clips 不应被 /media/{media_id} 之类的动态路由吞掉。
    response = client.get("/media-clips", headers=_auth(token))
    assert response.status_code == 200
    assert response.json()["total"] == 1
