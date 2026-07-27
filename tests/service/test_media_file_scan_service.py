from src.model import Media, MediaLibrary, Movie
from src.service.catalog.movie_subtitle_service import MovieSubtitleService
from src.service.playback.media_file_scan_service import MediaFileScanService


def _movie(sequence: int) -> Movie:
    return Movie.create(
        javdb_id=f"scan-movie-{sequence}",
        movie_number=f"SCAN-{sequence:03d}",
        title=f"Scan movie {sequence}",
    )


def _cloud115_library() -> MediaLibrary:
    return MediaLibrary.create(
        name="Cloud115",
        backend="cloud115",
        backend_config={"root_cid": "managed-root"},
    )


def _cloud115_media(
    library: MediaLibrary,
    *,
    sequence: int,
    pickcode: str,
    valid: bool,
) -> Media:
    return Media.create(
        movie=_movie(sequence),
        library=library,
        backend_locator={
            "fid": f"fid-{sequence}",
            "pickcode": pickcode,
            "name": f"SCAN-{sequence:03d}.mp4",
            "source_path": f"source-{sequence}",
        },
        valid=valid,
    )


def test_cloud115_scan_reconciles_valid_from_single_remote_index(
    test_db,
    monkeypatch,
) -> None:
    library = _cloud115_library()
    revived = _cloud115_media(
        library,
        sequence=1,
        pickcode="remote-present",
        valid=False,
    )
    invalidated = _cloud115_media(
        library,
        sequence=2,
        pickcode="remote-missing",
        valid=True,
    )

    monkeypatch.setattr(
        MediaFileScanService,
        "_build_cloud115_remote_index",
        staticmethod(lambda libraries: {library.id: {"remote-present"}}),
    )

    stats = MediaFileScanService().scan_media_files()

    assert stats == {
        "scanned_media": 2,
        "updated_media": 2,
        "skipped_media": 0,
        "failed_media": 0,
        "invalidated_media": 1,
        "revived_media": 1,
        "cloud115_index_failed_libraries": 0,
    }
    assert Media.get_by_id(revived.id).valid is True
    assert Media.get_by_id(invalidated.id).valid is False


def test_cloud115_scan_skips_library_when_remote_index_failed(
    test_db,
    monkeypatch,
) -> None:
    library = _cloud115_library()
    media = _cloud115_media(
        library,
        sequence=1,
        pickcode="unknown",
        valid=True,
    )
    monkeypatch.setattr(
        MediaFileScanService,
        "_build_cloud115_remote_index",
        staticmethod(lambda libraries: {}),
    )

    stats = MediaFileScanService().scan_media_files()

    assert stats["cloud115_index_failed_libraries"] == 1
    assert stats["updated_media"] == 0
    assert stats["skipped_media"] == 1
    assert Media.get_by_id(media.id).valid is True


def test_scan_excludes_media_created_after_remote_snapshot_started(
    test_db,
    monkeypatch,
) -> None:
    library = _cloud115_library()
    existing = _cloud115_media(
        library,
        sequence=1,
        pickcode="existing",
        valid=True,
    )
    late_media_id: list[int] = []

    def build_remote_index(libraries):
        late = _cloud115_media(
            library,
            sequence=2,
            pickcode="created-after-snapshot",
            valid=True,
        )
        late_media_id.append(late.id)
        return {library.id: {"existing"}}

    monkeypatch.setattr(
        MediaFileScanService,
        "_build_cloud115_remote_index",
        staticmethod(build_remote_index),
    )

    stats = MediaFileScanService().scan_media_files(batch_size=1)

    assert stats["scanned_media"] == 1
    assert Media.get_by_id(existing.id).valid is True
    assert Media.get_by_id(late_media_id[0]).valid is True


def test_media_file_scan_does_not_sync_subtitles(
    test_db,
    monkeypatch,
    tmp_path,
) -> None:
    media_path = tmp_path / "SCAN-001.mp4"
    media_path.touch()
    library = MediaLibrary.create(
        name="Local",
        backend="local",
        backend_config={"root_path": str(tmp_path)},
    )
    Media.create(
        movie=_movie(1),
        library=library,
        path=str(media_path),
        valid=True,
    )
    subtitle_sync_calls: list[str] = []
    monkeypatch.setattr(
        MovieSubtitleService,
        "sync_movie_subtitles",
        staticmethod(lambda movie: subtitle_sync_calls.append(movie.movie_number)),
    )

    stats = MediaFileScanService().scan_media_files()

    assert stats["scanned_media"] == 1
    assert subtitle_sync_calls == []
