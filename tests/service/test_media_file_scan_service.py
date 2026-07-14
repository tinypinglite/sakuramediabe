from pathlib import Path

import pytest

from src.api.exception.errors import ApiError
from src.model import Image, Media, MediaLibrary, Movie, MovieSeries, ResourceTaskState, Subtitle, VideoItem
from src.service.playback.media_file_scan_service import MediaFileScanService
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeResult


@pytest.fixture()
def media_file_scan_tables(test_db):
    models = [Image, MovieSeries, Movie, VideoItem, Subtitle, MediaLibrary, Media, ResourceTaskState]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_movie(movie_number: str, javdb_id: str):
    return Movie.create(movie_number=movie_number, javdb_id=javdb_id, title=movie_number)


class _FakeProbeService:
    def __init__(self, result: MediaMetadataProbeResult):
        self.result = result
        self.called_paths: list[Path] = []

    def probe_file(self, file_path: Path) -> MediaMetadataProbeResult:
        self.called_paths.append(file_path)
        return self.result


def _build_video_info(width: int, height: int) -> dict:
    return {
        "container": {"format_name": "mp4"},
        "video": {"codec_name": "h264", "profile": "Main", "width": width, "height": height},
        "audio": None,
        "subtitles": [],
    }


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


def test_scan_media_files_revives_media_and_backfills_video_info(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-302", "Movie302")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "abc-302.mp4"
    file_path.write_bytes(b"video-bytes")
    probe_result = MediaMetadataProbeResult(
        resolution="1920x1080",
        duration_seconds=300,
        video_info={
            "container": {"format_name": "mp4", "duration_seconds": 300, "bit_rate": 1000, "size_bytes": 11},
            "video": {"codec_name": "h264", "profile": "Main", "bit_rate": 800, "width": 1920, "height": 1080},
            "audio": {"codec_name": "aac", "profile": "LC", "bit_rate": 200, "sample_rate": 48000, "channels": 2},
            "subtitles": [],
        },
    )
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
    fake_probe = _FakeProbeService(probe_result)
    service = MediaFileScanService(metadata_probe_service=fake_probe)

    stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert stats["scanned_media"] == 1
    assert stats["updated_media"] == 1
    assert stats["invalidated_media"] == 0
    assert stats["revived_media"] == 1
    assert refreshed.valid is True
    assert refreshed.file_size_bytes == len(b"video-bytes")
    assert refreshed.resolution == "1920x1080"
    assert refreshed.duration_seconds == 300
    assert refreshed.video_info == probe_result.video_info
    assert refreshed.special_tags == "普通"
    assert fake_probe.called_paths == [file_path.resolve()]


def test_check_media_file_revives_media_and_backfills_video_info(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-312", "Movie312")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "ABC-312-4K-C.mp4"
    file_path.write_bytes(b"video-bytes")
    file_path.with_suffix(".srt").write_text("subtitle", encoding="utf-8")
    probe_result = MediaMetadataProbeResult(
        resolution="3840x2160",
        duration_seconds=600,
        video_info=_build_video_info(3840, 2160),
    )
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
    fake_probe = _FakeProbeService(probe_result)
    service = MediaFileScanService(metadata_probe_service=fake_probe)

    result = service.check_media_file(media.id)
    refreshed = Media.get_by_id(media.id)

    assert result.file_exists is True
    assert result.valid_before is False
    assert result.valid_after is True
    assert result.updated is True
    assert result.invalidated is False
    assert result.revived is True
    assert refreshed.valid is True
    assert refreshed.file_size_bytes == len(b"video-bytes")
    assert refreshed.resolution == "3840x2160"
    assert refreshed.duration_seconds == 600
    assert refreshed.video_info == probe_result.video_info
    assert refreshed.special_tags == "中字 4K"
    assert fake_probe.called_paths == [file_path.resolve()]


def test_scan_media_files_skips_probe_when_video_info_exists(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-303", "Movie303")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "abc-303.mp4"
    file_path.write_bytes(b"video-bytes")
    existing_video_info = {
        "container": {"format_name": "mp4"},
        "video": {"codec_name": "h264"},
        "audio": None,
        "subtitles": [],
    }
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=123,
        resolution="1280x720",
        duration_seconds=60,
        video_info=existing_video_info,
        valid=True,
    )
    fake_probe = _FakeProbeService(MediaMetadataProbeResult())
    service = MediaFileScanService(metadata_probe_service=fake_probe)

    stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert stats["scanned_media"] == 1
    assert stats["updated_media"] == 0
    assert stats["skipped_media"] == 1
    assert fake_probe.called_paths == []
    assert refreshed.video_info == existing_video_info


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
    fake_probe = _FakeProbeService(MediaMetadataProbeResult())
    service = MediaFileScanService(metadata_probe_service=fake_probe)

    result = service.check_media_file(media.id)

    assert result.file_exists is True
    assert result.valid_before is True
    assert result.valid_after is True
    assert result.updated is False
    assert result.invalidated is False
    assert result.revived is False
    assert fake_probe.called_paths == []


def test_check_media_file_returns_not_found_for_missing_media(media_file_scan_tables):
    service = MediaFileScanService()

    with pytest.raises(ApiError) as exc_info:
        service.check_media_file(999)

    assert exc_info.value.code == "media_not_found"


def test_scan_media_files_removes_4k_when_real_video_info_is_not_4k(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-305", "Movie305")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "ABC-305-4K.mp4"
    file_path.write_bytes(b"video-bytes")
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=len(b"video-bytes"),
        resolution="1920x1080",
        duration_seconds=60,
        video_info=_build_video_info(1920, 1080),
        special_tags="中字 4K 无码",
        valid=True,
    )
    service = MediaFileScanService()

    stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert stats["updated_media"] == 1
    assert refreshed.special_tags == "中字 无码"


def test_scan_media_files_adds_4k_when_real_video_info_is_4k(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-306", "Movie306")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "abc-306.mp4"
    file_path.write_bytes(b"video-bytes")
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=len(b"video-bytes"),
        resolution="3840x2160",
        duration_seconds=60,
        video_info=_build_video_info(3840, 2160),
        special_tags="普通",
        valid=True,
    )
    service = MediaFileScanService()

    stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert stats["updated_media"] == 1
    assert refreshed.special_tags == "4K"


def test_scan_media_files_is_idempotent_when_probe_returns_empty_video_info(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-304", "Movie304")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "abc-304.mp4"
    file_path.write_bytes(b"video-bytes")
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=len(b"video-bytes"),
        resolution=None,
        duration_seconds=0,
        video_info=None,
        valid=True,
    )
    fake_probe = _FakeProbeService(MediaMetadataProbeResult())
    service = MediaFileScanService(metadata_probe_service=fake_probe)

    first_stats = service.scan_media_files()
    second_stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert first_stats["updated_media"] == 0
    assert first_stats["skipped_media"] == 1
    assert second_stats["updated_media"] == 0
    assert second_stats["skipped_media"] == 1
    assert len(fake_probe.called_paths) == 2
    assert refreshed.video_info is None


def test_scan_media_files_rebuilds_special_tags_after_probe(media_file_scan_tables, tmp_path):
    movie = _create_movie("ABC-307", "Movie307")
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    file_path = tmp_path / "ABC-307-4K-C.mp4"
    file_path.write_bytes(b"video-bytes")
    file_path.with_suffix(".srt").write_text("subtitle", encoding="utf-8")
    probe_result = MediaMetadataProbeResult(
        resolution="3840x2160",
        duration_seconds=180,
        video_info=_build_video_info(3840, 2160),
    )
    media = Media.create(
        movie=movie,
        library=library,
        path=str(file_path),
        file_size_bytes=0,
        resolution=None,
        duration_seconds=0,
        video_info=None,
        special_tags="普通",
        valid=True,
    )
    fake_probe = _FakeProbeService(probe_result)
    service = MediaFileScanService(metadata_probe_service=fake_probe)

    stats = service.scan_media_files()
    refreshed = Media.get_by_id(media.id)

    assert stats["updated_media"] == 1
    assert refreshed.special_tags == "中字 4K"
    assert refreshed.video_info == probe_result.video_info


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
