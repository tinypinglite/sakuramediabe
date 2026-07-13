import pytest

from src.api.exception.errors import ApiError
from src.model import Image, Media, MediaLibrary, Movie, MovieSeries, ResourceTaskState, Subtitle, VideoItem
from src.service.playback.media_file_scan_service import MediaFileScanService


@pytest.fixture()
def media_file_scan_tables(test_db):
    models = [Image, MovieSeries, Movie, VideoItem, Subtitle, MediaLibrary, Media, ResourceTaskState]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_movie(movie_number: str, javdb_id: str):
    return Movie.create(movie_number=movie_number, javdb_id=javdb_id, title=movie_number)


def test_scan_media_files_invalidates_missing_media(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-301", "Movie301")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    media = Media.create(
        movie=movie,
        library=library,
        path=str(tmp_path / "missing.mp4"),
        valid=True,
    )
    service = MediaFileScanService()

    stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert stats["scanned_media"] == 1
    assert stats["updated_media"] == 1
    assert stats["invalidated_media"] == 1
    assert stats["revived_media"] == 0
    assert refreshed.valid is False


def test_check_media_file_invalidates_missing_media(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-311", "Movie311")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    media = Media.create(
        movie=movie,
        library=library,
        path=str(tmp_path / "missing.mp4"),
        valid=True,
    )
    service = MediaFileScanService()

    result = service.check_media_file(media.id)
    refreshed = Media.get_by_id(media.id)

    assert result.id == media.id
    assert result.path == str(tmp_path / "missing.mp4")
    assert result.file_exists is False
    assert result.valid_before is True
    assert result.valid_after is False
    assert result.updated is True
    assert result.invalidated is True
    assert result.revived is False
    assert refreshed.valid is False


def test_scan_media_files_revives_media_without_backfilling_metadata(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-302", "Movie302")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "abc-302.mp4"
    file_path.write_bytes(b"video-bytes")
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=0,
        resolution=None,
        duration_seconds=0,
        video_info=None,
        special_tags="普通",
        valid=False,
    )
    service = MediaFileScanService()

    stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert stats["scanned_media"] == 1
    assert stats["updated_media"] == 1
    assert stats["invalidated_media"] == 0
    assert stats["revived_media"] == 1
    assert refreshed.valid is True
    assert refreshed.file_size_bytes == 0
    assert refreshed.resolution is None
    assert refreshed.duration_seconds == 0
    assert refreshed.video_info is None
    assert refreshed.special_tags == "普通"


def test_check_media_file_revives_media_without_backfilling_metadata(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-312", "Movie312")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "ABC-312-4K-C.mp4"
    file_path.write_bytes(b"video-bytes")
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=0,
        resolution=None,
        duration_seconds=0,
        video_info=None,
        special_tags="普通",
        valid=False,
    )
    service = MediaFileScanService()

    result = service.check_media_file(media.id)
    refreshed = Media.get_by_id(media.id)

    assert result.file_exists is True
    assert result.valid_before is False
    assert result.valid_after is True
    assert result.updated is True
    assert result.invalidated is False
    assert result.revived is True
    assert refreshed.valid is True
    assert refreshed.file_size_bytes == 0
    assert refreshed.resolution is None
    assert refreshed.duration_seconds == 0
    assert refreshed.video_info is None
    assert refreshed.special_tags == "普通"


def test_check_media_file_returns_unchanged_when_valid_state_matches(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-313", "Movie313")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "abc-313.mp4"
    file_path.write_bytes(b"video-bytes")
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=len(b"video-bytes"),
        video_info={"container": {"format_name": "mp4"}, "video": None, "audio": None, "subtitles": []},
        valid=True,
    )
    service = MediaFileScanService()

    result = service.check_media_file(media.id)

    assert result.file_exists is True
    assert result.valid_before is True
    assert result.valid_after is True
    assert result.updated is False
    assert result.invalidated is False
    assert result.revived is False


def test_check_media_file_returns_not_found_for_missing_media(media_file_scan_tables):
    service = MediaFileScanService()

    with pytest.raises(ApiError) as exc_info:
        service.check_media_file(999)

    assert exc_info.value.code == "media_not_found"


# ---------------------------------------------------------------------------
# cloud115 媒体对账：pickcode 探活判存在性
# ---------------------------------------------------------------------------


def _create_cloud115_media(*, movie_number="ABC-115", valid=True) -> Media:
    movie = _create_movie(movie_number, f"javdb-{movie_number}")
    library = MediaLibrary.create(
        name=f"cloud-{movie_number}",
        backend="cloud115",
        backend_config={
            "cookies": "UID=12345678_A1_1700000000; CID=abc",
            "root_cid": "lib-root",
            "app": "alipaymini",
        },
    )
    return Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "f-1", "pickcode": "pc-115", "name": f"{movie_number}.mp4"},
        content_fingerprint="sha1:AAA",
        valid=valid,
    )


def _patch_cloud115_probe(monkeypatch, *, exc: Exception | None = None):
    """替换探活链路：exc=None 表示文件存在，否则抛出对应 SDK 异常。"""
    from contextlib import asynccontextmanager

    from src.service.playback import cloud115_backend_service as backend_module

    class _ProbeClient:
        async def pickcode_info(self, pickcode: str):
            if exc is not None:
                raise exc
            return object()

    @asynccontextmanager
    async def fake_client_for(_library):
        yield _ProbeClient()

    # _scan_cloud115_media 在函数体内 import cloud115_client_for → patch 源模块符号即可
    monkeypatch.setattr(backend_module, "cloud115_client_for", fake_client_for)
    import src.service.playback.cloud115_backend_service  # noqa: F401


def test_check_cloud115_media_invalidates_when_remote_gone(
    media_file_scan_tables, monkeypatch
):
    from src.lib.cloud115 import Cloud115NotFoundError

    media = _create_cloud115_media()
    _patch_cloud115_probe(monkeypatch, exc=Cloud115NotFoundError("pickcode invalid"))

    service = MediaFileScanService()
    result = service.check_media_file(media.id)

    assert result.file_exists is False
    assert result.invalidated is True
    assert Media.get_by_id(media.id).valid is False
    assert result.path == "cloud115:ABC-115.mp4"


def test_check_cloud115_media_revives_when_remote_back(
    media_file_scan_tables, monkeypatch
):
    media = _create_cloud115_media(valid=False)
    _patch_cloud115_probe(monkeypatch, exc=None)

    service = MediaFileScanService()
    result = service.check_media_file(media.id)

    assert result.file_exists is True
    assert result.revived is True
    assert Media.get_by_id(media.id).valid is True


def test_check_cloud115_media_skips_on_auth_error(media_file_scan_tables, monkeypatch):
    """cookies 失效 ≠ 文件没了：上游不可用时跳过本条、绝不误标 invalid。"""
    from src.lib.cloud115 import Cloud115AuthError

    media = _create_cloud115_media()
    _patch_cloud115_probe(monkeypatch, exc=Cloud115AuthError("cookies expired"))

    service = MediaFileScanService()
    result = service.check_media_file(media.id)

    assert result.invalidated is False
    assert result.updated is False
    assert Media.get_by_id(media.id).valid is True
