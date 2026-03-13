from fractions import Fraction
from pathlib import Path

import pytest

from src.model import Image, Media, MediaLibrary, MediaThumbnail, Movie


@pytest.fixture()
def thumbnail_tables(test_db):
    models = [Image, Movie, MediaLibrary, Media, MediaThumbnail]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_media(
    tmp_path: Path,
    *,
    fingerprint: str = "abc123",
    need_thumbnail_generation: bool = True,
    duration_seconds: int = 0,
    duration_minutes: int = 0,
) -> Media:
    movie = Movie.create(
        javdb_id="javdb-001",
        movie_number="ABC-001",
        title="Movie 1",
        duration_minutes=duration_minutes,
    )
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    video_path = tmp_path / "ABC-001.mp4"
    video_path.write_bytes(b"video")
    return Media.create(
        movie=movie,
        library=library,
        path=str(video_path),
        content_fingerprint=fingerprint,
        valid=True,
        need_thumbnail_generation=need_thumbnail_generation,
        duration_seconds=duration_seconds,
    )


def _write_webp_batch(target_dir: Path, offsets: list[int], *, valid_names: bool = True) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for offset in offsets:
        if valid_names:
            file_name = f"{offset}.webp"
        else:
            file_name = f"invalid_{offset}.webp"
        (target_dir / file_name).write_bytes(f"webp-{offset}".encode("utf-8"))


class _FakeImage:
    def __init__(self, saved_paths: list[tuple[str, str | None, int | None]]) -> None:
        self.saved_paths = saved_paths

    def save(self, path, format=None, quality=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"webp")
        self.saved_paths.append((path.name, format, quality))


class _FakeFrame:
    def __init__(self, saved_paths: list[tuple[str, str | None, int | None]]) -> None:
        self.saved_paths = saved_paths

    def to_image(self):
        return _FakeImage(self.saved_paths)


class _FakeStream:
    def __init__(self, *, duration, time_base) -> None:
        self.duration = duration
        self.time_base = time_base


class _FakeContainer:
    def __init__(
        self,
        *,
        stream_duration=None,
        stream_time_base=Fraction(1, 1),
        container_duration=None,
        seek_failures: set[int] | None = None,
        decode_failures: set[int] | None = None,
        close_error: Exception | None = None,
        video_streams: int = 1,
    ) -> None:
        self.streams = type("Streams", (), {})()
        self.streams.video = []
        if video_streams > 0:
            self.streams.video.append(
                _FakeStream(duration=stream_duration, time_base=stream_time_base)
            )
        self.duration = container_duration
        self.seek_failures = seek_failures or set()
        self.decode_failures = decode_failures or set()
        self.close_error = close_error
        self.saved_paths: list[tuple[str, str | None, int | None]] = []
        self.seek_calls: list[int] = []
        self.current_seek_seconds = 0

    def seek(self, timestamp, *, stream, backward, any_frame):
        seconds = int(timestamp * stream.time_base)
        self.seek_calls.append(seconds)
        self.current_seek_seconds = seconds
        if seconds in self.seek_failures:
            raise RuntimeError(f"seek failed at {seconds}")

    def decode(self, stream):
        if self.current_seek_seconds in self.decode_failures:
            raise RuntimeError(f"decode failed at {self.current_seek_seconds}")
        yield _FakeFrame(self.saved_paths)

    def close(self):
        if self.close_error is not None:
            raise self.close_error


def test_parse_offset_seconds_accepts_pure_second_filenames(thumbnail_tables, tmp_path):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    assert MediaThumbnailService._parse_offset_seconds(tmp_path / "10.webp") == 10
    assert MediaThumbnailService._parse_offset_seconds(tmp_path / "130.webp") == 130
    assert MediaThumbnailService._parse_offset_seconds(tmp_path / "11340.webp") == 11340


def test_parse_offset_seconds_rejects_legacy_filename_patterns(thumbnail_tables, tmp_path):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    assert MediaThumbnailService._parse_offset_seconds(tmp_path / "00_00_10.webp") is None
    assert (
        MediaThumbnailService._parse_offset_seconds(
            tmp_path / "IPX-759_o_01_22_59_00497.webp"
        )
        is None
    )


def test_collect_parseable_webp_files_sorts_by_numeric_offset(thumbnail_tables, tmp_path):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    webp_dir = tmp_path / "thumbnails"
    _write_webp_batch(webp_dir, [100, 20, 3])

    files, total_count = MediaThumbnailService._collect_parseable_webp_files(webp_dir)

    assert total_count == 3
    assert [file.name for file in files] == ["3.webp", "20.webp", "100.webp"]


def test_generate_pending_thumbnails_saves_webp_and_resets_media_state(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-a")
    image_root = tmp_path / "images"

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        assert interval_seconds == 10
        _write_webp_batch(webp_dir, [10, 20])
        return None

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()

    refreshed_media = Media.get_by_id(media.id)
    thumbnails = list(MediaThumbnail.select().order_by(MediaThumbnail.offset))

    assert stats["pending_media"] == 1
    assert stats["successful_media"] == 1
    assert stats["generated_thumbnails"] == 2
    assert refreshed_media.need_thumbnail_generation is False
    assert refreshed_media.thumbnail_retry_count == 0
    assert refreshed_media.thumbnail_last_error is None
    assert [thumbnail.offset for thumbnail in thumbnails] == [10, 20]
    assert thumbnails[0].image.origin == "movies/ABC-001/media/fingerprint-a/thumbnails/10.webp"


def test_generate_pending_thumbnails_cleans_stale_webp_before_generating(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    _create_media(tmp_path, fingerprint="fingerprint-clean")
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-clean" / "thumbnails"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "5.webp").write_bytes(b"stale")

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        assert not (webp_dir / "5.webp").exists()
        _write_webp_batch(webp_dir, [10])
        return None

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()

    assert stats["generated_thumbnails"] == 1
    assert MediaThumbnail.get().offset == 10
    assert not (target_dir / "5.webp").exists()


def test_generate_pending_thumbnails_treats_pyav_error_as_success_when_output_count_is_sufficient(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-b", duration_minutes=120)
    image_root = tmp_path / "images"

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        _write_webp_batch(webp_dir, list(range(612)))
        return RuntimeError("pyav-close-error")

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["successful_media"] == 1
    assert stats["generated_thumbnails"] == 612
    assert stats["retryable_failed_media"] == 0
    assert refreshed_media.need_thumbnail_generation is False
    assert refreshed_media.thumbnail_retry_count == 0
    assert refreshed_media.thumbnail_last_error is None


def test_generate_pending_thumbnails_falls_back_to_movie_duration_when_media_duration_is_missing(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(
        tmp_path,
        fingerprint="fingerprint-fallback",
        duration_seconds=0,
        duration_minutes=120,
    )
    image_root = tmp_path / "images"

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        _write_webp_batch(webp_dir, list(range(612)))
        return RuntimeError("pyav-close-error")

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()

    assert stats["successful_media"] == 1
    assert stats["generated_thumbnails"] == 612
    assert Media.get_by_id(media.id).thumbnail_last_error is None


def test_generate_pending_thumbnails_retries_then_marks_terminal_failure_when_output_count_is_insufficient(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-b", duration_minutes=120)
    image_root = tmp_path / "images"

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        _write_webp_batch(webp_dir, list(range(580)))
        return RuntimeError("pyav-close-error")

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    first = MediaThumbnailService.generate_pending_thumbnails()
    first_media = Media.get_by_id(media.id)
    second = MediaThumbnailService.generate_pending_thumbnails()
    second_media = Media.get_by_id(media.id)

    assert first["retryable_failed_media"] == 1
    assert first_media.need_thumbnail_generation is True
    assert first_media.thumbnail_retry_count == 1
    assert "thumbnail_generation_insufficient_count" in first_media.thumbnail_last_error
    assert "expected=720" in first_media.thumbnail_last_error
    assert "minimum=612" in first_media.thumbnail_last_error
    assert "actual=580" in first_media.thumbnail_last_error
    assert second["terminal_failed_media"] == 1
    assert second_media.need_thumbnail_generation is False
    assert second_media.thumbnail_retry_count == 2
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_keeps_strict_failure_when_duration_is_missing(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(
        tmp_path,
        fingerprint="fingerprint-no-duration",
        duration_seconds=0,
        duration_minutes=0,
    )
    image_root = tmp_path / "images"

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        _write_webp_batch(webp_dir, list(range(10)))
        return RuntimeError("pyav-close-error")

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["retryable_failed_media"] == 1
    assert refreshed_media.need_thumbnail_generation is True
    assert refreshed_media.thumbnail_retry_count == 1
    assert refreshed_media.thumbnail_last_error == "pyav-close-error"
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_marks_missing_fingerprint_as_terminal_failure(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint=None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["terminal_failed_media"] == 1
    assert refreshed_media.need_thumbnail_generation is False
    assert refreshed_media.thumbnail_last_error == "content_fingerprint_missing"
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_marks_parse_failure_with_specific_error(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-bad-name")
    image_root = tmp_path / "images"

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        _write_webp_batch(webp_dir, [10], valid_names=False)
        return None

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["retryable_failed_media"] == 1
    assert refreshed_media.need_thumbnail_generation is True
    assert refreshed_media.thumbnail_last_error == "thumbnail_generation_unparseable_filenames"
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_does_not_count_unparseable_files_towards_tolerant_success(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-unparseable-many", duration_minutes=120)
    image_root = tmp_path / "images"

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        _write_webp_batch(webp_dir, list(range(620)), valid_names=False)
        return RuntimeError("pyav-close-error")

    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["retryable_failed_media"] == 1
    assert refreshed_media.need_thumbnail_generation is True
    assert "thumbnail_generation_insufficient_count" in refreshed_media.thumbnail_last_error
    assert "actual=0" in refreshed_media.thumbnail_last_error
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_logs_flow_for_success_and_terminal_failure(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    success_media = _create_media(tmp_path, fingerprint="fingerprint-success")
    failed_media = _create_media(tmp_path, fingerprint=None)  # type: ignore[arg-type]
    image_root = tmp_path / "images"
    events: list[tuple[str, str]] = []

    class FakeLogger:
        def info(self, message, *args):
            events.append(("info", message.format(*args) if args else message))

        def warning(self, message, *args):
            events.append(("warning", message.format(*args) if args else message))

    def fake_generate(video_path, webp_dir, *, interval_seconds=10):
        _write_webp_batch(webp_dir, [10])
        return None

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.logger", FakeLogger())
    monkeypatch.setattr(
        MediaThumbnailService,
        "_generate_webp_with_pyav",
        staticmethod(fake_generate),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.import_image_root_path",
        str(image_root),
    )
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.settings.media.max_thumbnail_process_count",
        1,
    )

    stats = MediaThumbnailService.generate_pending_thumbnails()

    assert stats == {
        "pending_media": 2,
        "successful_media": 1,
        "generated_thumbnails": 1,
        "retryable_failed_media": 0,
        "terminal_failed_media": 1,
    }
    assert any(
        level == "info" and "Starting media thumbnail generation pending_media=2 max_workers=1" in message
        for level, message in events
    )
    assert any(
        level == "info" and f"Generating media thumbnails media_id={success_media.id}" in message
        for level, message in events
    )
    assert any(
        level == "info"
        and f"Generated media thumbnails media_id={success_media.id} generated_thumbnails=1" in message
        for level, message in events
    )
    assert any(
        level == "warning"
        and f"Generate media thumbnails aborted media_id={failed_media.id} reason=content_fingerprint_missing failure_type=terminal"
        in message
        for level, message in events
    )
    assert any(
        level == "info"
        and "Finished media thumbnail generation pending_media=2 successful_media=1 generated_thumbnails=1 retryable_failed_media=0 terminal_failed_media=1"
        in message
        for level, message in events
    )


def test_generate_webp_with_pyav_saves_pure_second_webp_files(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    container = _FakeContainer(stream_duration=21, stream_time_base=Fraction(1, 1))
    av_module = type("FakeAv", (), {"open": staticmethod(lambda path: container), "time_base": 1})
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.av", av_module)

    webp_dir = tmp_path / "webp"
    error = MediaThumbnailService._generate_webp_with_pyav(
        tmp_path / "video.mp4",
        webp_dir,
        interval_seconds=10,
    )

    assert error is None
    assert [item[0] for item in container.saved_paths] == ["0.webp", "10.webp", "20.webp"]
    assert all(item[1] == "WEBP" for item in container.saved_paths)
    assert all(item[2] == 80 for item in container.saved_paths)


def test_generate_webp_with_pyav_uses_container_duration_when_stream_duration_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    container = _FakeContainer(
        stream_duration=None,
        stream_time_base=Fraction(1, 1),
        container_duration=21,
    )
    av_module = type("FakeAv", (), {"open": staticmethod(lambda path: container), "time_base": 1})
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.av", av_module)

    webp_dir = tmp_path / "webp"
    error = MediaThumbnailService._generate_webp_with_pyav(
        tmp_path / "video.mp4",
        webp_dir,
        interval_seconds=10,
    )

    assert error is None
    assert [item[0] for item in container.saved_paths] == ["0.webp", "10.webp", "20.webp"]


def test_generate_webp_with_pyav_skips_failed_offsets_and_returns_close_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    container = _FakeContainer(
        stream_duration=31,
        stream_time_base=Fraction(1, 1),
        seek_failures={10},
        decode_failures={20},
        close_error=RuntimeError("close failed"),
    )
    av_module = type("FakeAv", (), {"open": staticmethod(lambda path: container), "time_base": 1})
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.av", av_module)

    webp_dir = tmp_path / "webp"
    error = MediaThumbnailService._generate_webp_with_pyav(
        tmp_path / "video.mp4",
        webp_dir,
        interval_seconds=10,
    )

    assert str(error) == "close failed"
    assert [item[0] for item in container.saved_paths] == ["0.webp", "30.webp"]


def test_generate_webp_with_pyav_returns_error_when_video_stream_is_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    container = _FakeContainer(video_streams=0)
    av_module = type("FakeAv", (), {"open": staticmethod(lambda path: container), "time_base": 1})
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.av", av_module)

    error = MediaThumbnailService._generate_webp_with_pyav(
        tmp_path / "video.mp4",
        tmp_path / "webp",
        interval_seconds=10,
    )

    assert str(error) == "video_stream_missing"


def test_list_media_thumbnails_returns_sorted_resources(thumbnail_tables, tmp_path):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, need_thumbnail_generation=False)
    second_image = Image.create(
        origin="movies/ABC-001/media/abc123/thumbnails/20.webp",
        small="movies/ABC-001/media/abc123/thumbnails/20.webp",
        medium="movies/ABC-001/media/abc123/thumbnails/20.webp",
        large="movies/ABC-001/media/abc123/thumbnails/20.webp",
    )
    first_image = Image.create(
        origin="movies/ABC-001/media/abc123/thumbnails/10.webp",
        small="movies/ABC-001/media/abc123/thumbnails/10.webp",
        medium="movies/ABC-001/media/abc123/thumbnails/10.webp",
        large="movies/ABC-001/media/abc123/thumbnails/10.webp",
    )
    MediaThumbnail.create(media=media, image=second_image, offset=20)
    MediaThumbnail.create(media=media, image=first_image, offset=10)

    items = MediaThumbnailService.list_media_thumbnails(media.id)

    assert [item.offset_seconds for item in items] == [10, 20]
    assert [item.thumbnail_id for item in items] == [2, 1]
