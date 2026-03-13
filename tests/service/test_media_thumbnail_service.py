import subprocess
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
    need_mtn: bool = True,
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
        need_mtn=need_mtn,
        duration_seconds=duration_seconds,
    )


def _write_webp_batch(target_dir: Path, count: int, *, valid_names: bool = True) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        if valid_names:
            minutes, seconds = divmod(index, 60)
            file_name = f"00_{minutes:02d}_{seconds:02d}.webp"
        else:
            file_name = f"invalid_{index:04d}.webp"
        (target_dir / file_name).write_bytes(b"webp")


def test_generate_pending_thumbnails_saves_webp_and_resets_media_state(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-a")
    image_root = tmp_path / "images"
    commands: list[tuple[str | tuple[str, ...], bool]] = []

    def fake_run(command, *args, **kwargs):
        shell = kwargs.get("shell", False)
        commands.append((command if shell else tuple(command), shell))
        if shell:
            target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-a" / "thumbnails"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "00_00_10.webp").write_bytes(b"webp-1")
            (target_dir / "00_00_20.webp").write_bytes(b"webp-2")
        else:
            png_dir = Path(command[command.index("-O") + 1])
            png_dir.mkdir(parents=True, exist_ok=True)
            (png_dir / "00_00_10.png").write_bytes(b"png-1")
            (png_dir / "00_00_20.png").write_bytes(b"png-2")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.thumbnail_mtn_path", "custom-mtn")
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()

    refreshed_media = Media.get_by_id(media.id)
    thumbnails = list(MediaThumbnail.select().order_by(MediaThumbnail.offset))

    assert stats["pending_media"] == 1
    assert stats["successful_media"] == 1
    assert stats["generated_thumbnails"] == 2
    assert refreshed_media.need_mtn is False
    assert refreshed_media.mtn_retry_count == 0
    assert refreshed_media.mtn_last_error is None
    assert [thumbnail.offset for thumbnail in thumbnails] == [10, 20]
    assert all(thumbnail.image.origin.endswith(".webp") for thumbnail in thumbnails)
    assert thumbnails[0].image.origin == "movies/ABC-001/media/fingerprint-a/thumbnails/00_00_10.webp"
    assert commands[0][0][0] == "custom-mtn"
    assert commands[1][1] is True
    assert "mogrify -path" in commands[1][0]
    assert "/*" in commands[1][0]


def test_generate_pending_thumbnails_cleans_stale_webp_before_converting(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-clean")
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-clean" / "thumbnails"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "00_00_05.webp").write_bytes(b"stale")

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            (target_dir / "00_00_10.webp").write_bytes(b"new")
        else:
            png_dir = Path(command[command.index("-O") + 1])
            png_dir.mkdir(parents=True, exist_ok=True)
            (png_dir / "00_00_10.png").write_bytes(b"png-1")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()

    assert stats["generated_thumbnails"] == 1
    assert MediaThumbnail.select().count() == 1
    assert MediaThumbnail.get().offset == 10


def test_generate_pending_thumbnails_persists_real_mtn_filename_pattern(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-real")
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-real" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "IPVR-099_o_00_00_13_00000.webp").write_bytes(b"webp-1")
        else:
            png_dir = Path(command[command.index("-O") + 1])
            png_dir.mkdir(parents=True, exist_ok=True)
            (png_dir / "IPVR-099_o_00_00_13_00000.png").write_bytes(b"png-1")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)
    thumbnail = MediaThumbnail.get()

    assert stats["successful_media"] == 1
    assert stats["generated_thumbnails"] == 1
    assert refreshed_media.need_mtn is False
    assert refreshed_media.mtn_last_error is None
    assert thumbnail.offset == 13
    assert thumbnail.image.origin.endswith("IPVR-099_o_00_00_13_00000.webp")


def test_generate_pending_thumbnails_supports_multiple_real_mtn_files(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    _create_media(tmp_path, fingerprint="fingerprint-many")
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-many" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "IPVR-099_o_00_01_41_00009.webp").write_bytes(b"webp-2")
            (target_dir / "IPVR-099_o_00_00_13_00000.webp").write_bytes(b"webp-1")
            (target_dir / "IPVR-099_o_00_02_10_00012.webp").write_bytes(b"webp-3")
        else:
            png_dir = Path(command[command.index("-O") + 1])
            png_dir.mkdir(parents=True, exist_ok=True)
            (png_dir / "IPVR-099_o_00_00_13_00000.png").write_bytes(b"png-1")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()
    thumbnails = list(MediaThumbnail.select().order_by(MediaThumbnail.offset.asc()))

    assert stats["generated_thumbnails"] == 3
    assert [item.offset for item in thumbnails] == [13, 101, 130]


def test_generate_pending_thumbnails_treats_non_zero_mtn_exit_as_success_when_output_count_is_sufficient(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-b", duration_minutes=120)
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-b" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            _write_webp_batch(target_dir, 612)
            return subprocess.CompletedProcess(command, 0)
        png_dir = Path(command[command.index("-O") + 1])
        png_dir.mkdir(parents=True, exist_ok=True)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["successful_media"] == 1
    assert stats["generated_thumbnails"] == 612
    assert stats["retryable_failed_media"] == 0
    assert refreshed_media.need_mtn is False
    assert refreshed_media.mtn_retry_count == 0
    assert refreshed_media.mtn_last_error is None
    assert MediaThumbnail.select().count() == 612


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
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-fallback" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            _write_webp_batch(target_dir, 612)
            return subprocess.CompletedProcess(command, 0)
        png_dir = Path(command[command.index("-O") + 1])
        png_dir.mkdir(parents=True, exist_ok=True)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()

    assert stats["successful_media"] == 1
    assert stats["generated_thumbnails"] == 612
    assert Media.get_by_id(media.id).mtn_last_error is None


def test_generate_pending_thumbnails_retries_then_marks_terminal_failure_when_output_count_is_insufficient(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-b", duration_minutes=120)
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-b" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            _write_webp_batch(target_dir, 580)
            return subprocess.CompletedProcess(command, 0)
        png_dir = Path(command[command.index("-O") + 1])
        png_dir.mkdir(parents=True, exist_ok=True)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    first = MediaThumbnailService.generate_pending_thumbnails()
    first_media = Media.get_by_id(media.id)
    second = MediaThumbnailService.generate_pending_thumbnails()
    second_media = Media.get_by_id(media.id)

    assert first["retryable_failed_media"] == 1
    assert first_media.need_mtn is True
    assert first_media.mtn_retry_count == 1
    assert "thumbnail_generation_insufficient_count" in first_media.mtn_last_error
    assert "expected=720" in first_media.mtn_last_error
    assert "minimum=612" in first_media.mtn_last_error
    assert "actual=580" in first_media.mtn_last_error
    assert second["terminal_failed_media"] == 1
    assert second_media.need_mtn is False
    assert second_media.mtn_retry_count == 2
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
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-no-duration" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            _write_webp_batch(target_dir, 612)
            return subprocess.CompletedProcess(command, 0)
        png_dir = Path(command[command.index("-O") + 1])
        png_dir.mkdir(parents=True, exist_ok=True)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["retryable_failed_media"] == 1
    assert refreshed_media.need_mtn is True
    assert refreshed_media.mtn_retry_count == 1
    assert "non-zero exit status 1" in refreshed_media.mtn_last_error
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_marks_missing_fingerprint_as_terminal_failure(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint=None)  # type: ignore[arg-type]
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["terminal_failed_media"] == 1
    assert refreshed_media.need_mtn is False
    assert refreshed_media.mtn_last_error == "content_fingerprint_missing"
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_marks_parse_failure_with_specific_error(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-bad-name")
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-bad-name" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "IPVR-099_cover.webp").write_bytes(b"webp-1")
        else:
            png_dir = Path(command[command.index("-O") + 1])
            png_dir.mkdir(parents=True, exist_ok=True)
            (png_dir / "IPVR-099_cover.png").write_bytes(b"png-1")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["retryable_failed_media"] == 1
    assert refreshed_media.need_mtn is True
    assert refreshed_media.mtn_last_error == "thumbnail_generation_unparseable_filenames"
    assert MediaThumbnail.select().count() == 0


def test_generate_pending_thumbnails_does_not_count_unparseable_files_towards_tolerant_success(
    thumbnail_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, fingerprint="fingerprint-unparseable-many", duration_minutes=120)
    image_root = tmp_path / "images"
    target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-unparseable-many" / "thumbnails"

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            _write_webp_batch(target_dir, 620, valid_names=False)
            return subprocess.CompletedProcess(command, 0)
        png_dir = Path(command[command.index("-O") + 1])
        png_dir.mkdir(parents=True, exist_ok=True)
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

    stats = MediaThumbnailService.generate_pending_thumbnails()
    refreshed_media = Media.get_by_id(media.id)

    assert stats["retryable_failed_media"] == 1
    assert refreshed_media.need_mtn is True
    assert "thumbnail_generation_insufficient_count" in refreshed_media.mtn_last_error
    assert "actual=0" in refreshed_media.mtn_last_error
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

    def fake_run(command, *args, **kwargs):
        if kwargs.get("shell", False):
            target_dir = image_root / "movies" / "ABC-001" / "media" / "fingerprint-success" / "thumbnails"
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "00_00_10.webp").write_bytes(b"webp-1")
        else:
            png_dir = Path(command[command.index("-O") + 1])
            png_dir.mkdir(parents=True, exist_ok=True)
            (png_dir / "00_00_10.png").write_bytes(b"png-1")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("src.service.playback.media_thumbnail_service.logger", FakeLogger())
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.subprocess.run", fake_run)
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.import_image_root_path", str(image_root))
    monkeypatch.setattr("src.service.playback.media_thumbnail_service.settings.media.max_mtn_process_count", 1)

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


def test_list_media_thumbnails_returns_sorted_resources(thumbnail_tables, tmp_path):
    from src.service.playback.media_thumbnail_service import MediaThumbnailService

    media = _create_media(tmp_path, need_mtn=False)
    second_image = Image.create(
        origin="movies/ABC-001/media/abc123/thumbnails/00_00_20.webp",
        small="movies/ABC-001/media/abc123/thumbnails/00_00_20.webp",
        medium="movies/ABC-001/media/abc123/thumbnails/00_00_20.webp",
        large="movies/ABC-001/media/abc123/thumbnails/00_00_20.webp",
    )
    first_image = Image.create(
        origin="movies/ABC-001/media/abc123/thumbnails/00_00_10.webp",
        small="movies/ABC-001/media/abc123/thumbnails/00_00_10.webp",
        medium="movies/ABC-001/media/abc123/thumbnails/00_00_10.webp",
        large="movies/ABC-001/media/abc123/thumbnails/00_00_10.webp",
    )
    MediaThumbnail.create(media=media, image=second_image, offset=20)
    MediaThumbnail.create(media=media, image=first_image, offset=10)

    items = MediaThumbnailService.list_media_thumbnails(media.id)

    assert [item.offset_seconds for item in items] == [10, 20]
    assert [item.thumbnail_id for item in items] == [2, 1]
