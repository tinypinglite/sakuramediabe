import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import pytest

from src.config.config import settings
from src.metadata.provider import MetadataNotFoundError
from src.model import (
    Actor,
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    Image,
    ImageSearchSession,
    ImportJob,
    Media,
    MediaLibrary,
    MediaPoint,
    MediaProgress,
    MediaThumbnail,
    Movie,
    MovieActor,
    MoviePlotImage,
    MovieSeries,
    MovieTag,
    ResourceTaskState,
    Subtitle,
    Tag,
)
from sakuramedia_metadata_providers.models import JavdbMovieActorResource, JavdbMovieDetailResource, JavdbMovieTagResource
from src.service.catalog import ImageDownloadError
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeResult
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.system.resource_task_state_service import ResourceTaskStateService
from src.service.transfers.media_import_service import MediaImportService


@pytest.fixture()
def import_tables(test_db):
    models = [
        Image,
        Tag,
        Actor,
        MovieSeries,
        Movie,
        MovieActor,
        MovieTag,
        MoviePlotImage,
        Subtitle,
        MediaLibrary,
        DownloadClient,
        DownloadTask,
        BackgroundTaskRun,
        ImportJob,
        Media,
        MediaThumbnail,
        MediaProgress,
        MediaPoint,
        ImageSearchSession,
        ResourceTaskState,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


class FakeJavdbProvider:
    def __init__(self, details: Dict[str, JavdbMovieDetailResource], failures: Dict[str, Exception] | None = None):
        self.details = details
        self.failures = failures or {}

    def get_movie_by_number(self, movie_number: str) -> JavdbMovieDetailResource:
        if movie_number in self.failures:
            raise self.failures[movie_number]
        return self.details[movie_number]


def _build_detail(movie_number: str) -> JavdbMovieDetailResource:
    return JavdbMovieDetailResource(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=f"title-{movie_number}",
        cover_image="https://example.com/cover.jpg",
        release_date="2024-05-20",
        duration_minutes=120,
        score=4.4,
        watched_count=11,
        want_watch_count=12,
        comment_count=13,
        score_number=14,
        is_subscribed=False,
        summary="summary",
        series_name="series",
        actors=[
            JavdbMovieActorResource(
                javdb_id=f"actor-{movie_number}",
                name="actor-a",
                avatar_url="https://example.com/actor-a.jpg",
            )
        ],
        tags=[
            JavdbMovieTagResource(javdb_id=f"tag-{movie_number}", name="剧情"),
        ],
        plot_images=[
            "https://example.com/plot-1.jpg",
            "https://example.com/plot-2.jpg",
        ],
    )


def _fake_downloader(url: str, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(f"downloaded:{url}".encode("utf-8"))


def _read_failed_files(job: ImportJob) -> List[Dict[str, str]]:
    return json.loads(job.failed_files)


class SpyCatalogImportService:
    def __init__(self):
        self.called_movie_numbers: List[str] = []
        self.force_subscribed_args: List[bool] = []

    def upsert_movie_from_javdb_detail(
        self,
        detail: JavdbMovieDetailResource,
        force_subscribed: bool = False,
    ) -> Movie:
        self.called_movie_numbers.append(detail.movie_number)
        self.force_subscribed_args.append(force_subscribed)
        movie, _ = Movie.get_or_create(
            movie_number=detail.movie_number,
            defaults={
                "javdb_id": detail.javdb_id,
                "title": detail.title,
            },
        )
        return movie


class FakeMediaMetadataProbeService:
    def __init__(
        self,
        *,
        resolution: str | None,
        duration_seconds: int,
        video_info: dict | None = None,
    ):
        self.resolution = resolution
        self.duration_seconds = duration_seconds
        self.video_info = video_info

    def probe_file(self, _file_path: Path) -> MediaMetadataProbeResult:
        return MediaMetadataProbeResult(
            resolution=self.resolution,
            duration_seconds=self.duration_seconds,
            video_info=self.video_info,
        )


def _build_video_info(width: int, height: int) -> dict:
    return {
        "container": {"format_name": "mp4"},
        "video": {"codec_name": "h264", "profile": "Main", "width": width, "height": height},
        "audio": None,
        "subtitles": [],
    }


def test_import_media_groups_by_number_and_creates_one_version_per_video(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    first = source_dir / "ABP-123-C-4K-UC.mkv"
    second = source_dir / "abp123-normal.mp4"
    first_subtitle = source_dir / "ABP-123-C-4K-UC.srt"
    first.write_bytes(b"x" * 10)
    second.write_bytes(b"y" * 10)
    first_subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")

    library_root = tmp_path / "library"
    image_root = tmp_path / "images"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(image_root))

    provider = FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")})
    service = MediaImportService(
        provider=provider,
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
        media_metadata_probe_service=FakeMediaMetadataProbeService(
            resolution="1920x1080",
            duration_seconds=120,
            video_info=_build_video_info(1920, 1080),
        ),
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 2
    assert job.failed_count == 0
    assert job.skipped_count == 0

    media_items = list(Media.select().order_by(Media.id))
    assert len(media_items) == 2
    parent_directories = {Path(item.path).parent.name for item in media_items}
    assert len(parent_directories) == 2
    assert all(Path(item.path).parent.parent.name == "ABP-123" for item in media_items)
    assert {Path(item.path).name for item in media_items} == {"ABP-123.mkv", "ABP-123.mp4"}
    subtitle_paths = sorted(path.relative_to(library_root).as_posix() for path in library_root.rglob("*.srt"))
    assert subtitle_paths == ["ABP-123/1730000000000/ABP-123.srt"]
    assert set(media.special_tags for media in media_items) == {"中字 无码", "普通"}
    assert set(media.storage_mode for media in media_items).issubset({"hardlink", "copy"})

    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.cover_image_id is not None
    assert movie.cover_image is not None
    assert movie.cover_image.origin == "movies/ABP-123/cover.jpg"
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None
    assert Actor.select().count() == 1
    actor = Actor.select().first()
    assert actor is not None
    assert actor.profile_image is not None
    assert Path(actor.profile_image.origin).parts[0] == "actors"
    assert Path(actor.profile_image.origin).stem == actor.javdb_id
    assert Tag.select().count() == 1
    assert MovieActor.select().count() == 1
    assert MovieTag.select().count() == 1
    assert MoviePlotImage.select().count() == 2
    plot_image_paths = [
        link.image.origin
        for link in MoviePlotImage.select(MoviePlotImage, Image).join(Image).order_by(MoviePlotImage.id)
    ]
    assert plot_image_paths == [
        "movies/ABP-123/plots/0.jpg",
        "movies/ABP-123/plots/1.jpg",
    ]

    image = Image.select().first()
    assert image is not None
    assert not Path(image.origin).is_absolute()
    assert image.origin == image.small == image.medium == image.large


def test_import_media_marks_4k_from_real_video_info_even_when_file_name_has_no_4k(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-124-plain.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-124": _build_detail("ABP-124")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
        media_metadata_probe_service=FakeMediaMetadataProbeService(
            resolution="3840x2160",
            duration_seconds=120,
            video_info=_build_video_info(3840, 2160),
        ),
    )

    service.import_from_source(str(source_dir), library.id)

    media = Media.get()
    assert media.special_tags == "4K"


def test_import_media_reuses_existing_import_job_and_updates_download_task_status(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    (source_dir / "ABP-123.srt").write_text("subtitle", encoding="utf-8")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    client = DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path=str(tmp_path / "downloads"),
        media_library=library,
    )
    task = DownloadTask.create(
        client=client,
        movie="ABP-123",
        name="ABP-123",
        info_hash="hash-1",
        save_path=str(source_dir),
        progress=1.0,
        download_state="completed",
        import_status="pending",
    )
    job = ImportJob.create(
        source_path=str(source_dir),
        library=library,
        download_task=task,
        state="pending",
    )

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
    )

    result = service.import_from_source(
        str(source_dir),
        library.id,
        download_task_id=task.id,
        import_job_id=job.id,
    )
    task = DownloadTask.get_by_id(task.id)

    assert result.id == job.id
    assert task.import_status == "completed"
    imported_subtitles = sorted((tmp_path / "library").rglob("*.srt"))
    assert len(imported_subtitles) == 1
    assert imported_subtitles[0].name == "ABP-123.srt"


def test_import_media_ignores_non_srt_sidecar_subtitles(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    (source_dir / "ABP-123.ass").write_text("ignored", encoding="utf-8")
    library_root = tmp_path / "library"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert list(library_root.rglob("*.srt")) == []


def test_import_media_accepts_single_file_source_path(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    target_file = source_dir / "ABP-123.mp4"
    target_file.write_bytes(b"x" * 10)
    (source_dir / "ABP-124.mp4").write_bytes(b"y" * 10)
    library_root = tmp_path / "library"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(target_file), library.id)

    assert job.state == "completed"
    assert job.imported_count == 1
    assert Movie.select().count() == 1
    assert Movie.get().movie_number == "ABP-123"
    assert [Path(media.path).name for media in Media.select()] == ["ABP-123.mp4"]


def test_import_media_skips_small_files(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123-small.mp4").write_bytes(b"x")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1024)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
    )
    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 0
    assert job.skipped_count == 1
    assert job.failed_count == 0
    assert any(item["reason"] == "file_too_small" for item in _read_failed_files(job))


def test_import_media_records_failures_and_continues(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "no_number_here.mkv").write_bytes(b"x" * 10)
    (source_dir / "ABP-124.mkv").write_bytes(b"y" * 10)
    (source_dir / "ABP-123.mp4").write_bytes(b"z" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider(
            {"ABP-123": _build_detail("ABP-123")},
            failures={"ABP-124": MetadataNotFoundError("movie", "ABP-124")},
        ),
        image_downloader=_fake_downloader,
    )
    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "failed"
    assert job.imported_count == 1
    assert job.failed_count == 2
    reasons = [item["reason"] for item in _read_failed_files(job)]
    assert "movie_number_not_found" in reasons
    assert "metadata_fetch_failed" in reasons


def test_import_media_records_image_download_failed_reason(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    def _broken_downloader(url: str, target_path: Path) -> None:
        raise ImageDownloadError("network down")

    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_broken_downloader,
    )
    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "failed"
    assert job.imported_count == 0
    assert job.failed_count == 1
    assert any(item["reason"] == "image_download_failed" for item in _read_failed_files(job))


def test_import_media_is_idempotent_for_catalog_links(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    provider = FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")})
    service = MediaImportService(
        provider=provider,
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    first_job = service.import_from_source(str(source_dir), library.id)
    second_job = service.import_from_source(str(source_dir), library.id)

    assert first_job.state == "completed"
    assert second_job.state == "completed"
    assert Movie.select().count() == 1
    assert Actor.select().count() == 1
    assert Tag.select().count() == 1
    assert MovieActor.select().count() == 1
    assert MovieTag.select().count() == 1
    assert Media.select().count() == 1


def test_import_media_counts_duplicate_source_file_as_skipped(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    video_path = source_dir / "ABP-123.mp4"
    video_path.write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    first_job = service.import_from_source(str(source_dir), library.id)
    second_job = service.import_from_source(str(source_dir), library.id)

    assert first_job.imported_count == 1
    assert second_job.state == "completed"
    assert second_job.imported_count == 0
    assert second_job.skipped_count == 1
    assert second_job.failed_count == 0
    assert _read_failed_files(second_job) == []
    assert Media.select().count() == 1


def test_import_media_counts_same_batch_duplicate_file_as_skipped(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    first = source_dir / "ABP-123-a.mp4"
    second = source_dir / "ABP-123-b.mp4"
    shared_bytes = b"x" * 10
    first.write_bytes(shared_bytes)
    second.write_bytes(shared_bytes)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 1
    assert job.skipped_count == 1
    assert job.failed_count == 0
    assert _read_failed_files(job) == []
    assert Media.select().count() == 1


def test_import_media_duplicate_check_happens_before_version_directory_creation(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    library_root = tmp_path / "library"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    service.import_from_source(str(source_dir), library.id)
    first_versions = sorted(path.name for path in (library_root / "ABP-123").iterdir() if path.is_dir())
    service.import_from_source(str(source_dir), library.id)
    second_versions = sorted(path.name for path in (library_root / "ABP-123").iterdir() if path.is_dir())

    assert first_versions == ["1730000000000"]
    assert second_versions == ["1730000000000"]


def test_import_media_still_imports_when_same_movie_number_but_different_source_file(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    first = source_dir / "ABP-123-cd1.mp4"
    second = source_dir / "ABP-123-cd2.mp4"
    first.write_bytes(b"x" * 10)
    second.write_bytes(b"y" * 11)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 2
    assert Media.select().count() == 2


def test_content_fingerprint_is_saved_on_media_record(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    service.import_from_source(str(source_dir), library.id)

    media = Media.get()
    assert media.content_fingerprint
    assert ":" not in media.content_fingerprint
    thumbnail_task_state = ResourceTaskStateService.get_state(
        MediaThumbnailService.TASK_KEY,
        media.id,
    )
    assert thumbnail_task_state is not None
    assert thumbnail_task_state.state == "pending"
    assert thumbnail_task_state.attempt_count == 0
    assert thumbnail_task_state.last_error is None


def test_import_media_persists_probe_metadata_on_create(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-190.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    expected_video_info = {
        "container": {"format_name": "mp4", "duration_seconds": 3666, "bit_rate": 4200000, "size_bytes": 10},
        "video": {"codec_name": "h264", "profile": "Main", "bit_rate": 3800000, "width": 1920, "height": 1080},
        "audio": {"codec_name": "aac", "profile": "LC", "bit_rate": 192000, "sample_rate": 48000, "channels": 2},
        "subtitles": [],
    }
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-190": _build_detail("ABP-190")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
        media_metadata_probe_service=FakeMediaMetadataProbeService(
            resolution="1920x1080",
            duration_seconds=3666,
            video_info=expected_video_info,
        ),
    )

    job = service.import_from_source(str(source_dir), library.id)
    media = Media.get()

    assert job.state == "completed"
    assert media.resolution == "1920x1080"
    assert media.duration_seconds == 3666
    assert media.video_info == expected_video_info


def test_import_media_revive_invalid_media_persists_probe_metadata(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-191.mp4").write_bytes(b"x" * 10)
    old_path = tmp_path / "old-library" / "ABP-191" / "video.mp4"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    movie = Movie.create(javdb_id="javdb-ABP-191", movie_number="ABP-191", title="old-title")
    invalid_media = Media.create(
        movie=movie,
        library=library,
        path=str(old_path),
        storage_mode="copy",
        content_fingerprint="stale",
        file_size_bytes=3,
        resolution=None,
        duration_seconds=0,
        valid=False,
    )

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    expected_video_info = {
        "container": {"format_name": "mp4", "duration_seconds": 5400, "bit_rate": 5200000, "size_bytes": 10},
        "video": {"codec_name": "h264", "profile": "High", "bit_rate": 5000000, "width": 3840, "height": 2160},
        "audio": None,
        "subtitles": [],
    }
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-191": _build_detail("ABP-191")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
        media_metadata_probe_service=FakeMediaMetadataProbeService(
            resolution="3840x2160",
            duration_seconds=5400,
            video_info=expected_video_info,
        ),
    )

    expected_fingerprint = service._build_content_fingerprint(source_dir / "ABP-191.mp4", "ABP-191")
    invalid_media.content_fingerprint = expected_fingerprint
    invalid_media.save(only=[Media.content_fingerprint])

    job = service.import_from_source(str(source_dir), library.id)
    revived_media = Media.get_by_id(invalid_media.id)

    assert job.state == "completed"
    assert revived_media.resolution == "3840x2160"
    assert revived_media.duration_seconds == 5400
    assert revived_media.video_info == expected_video_info


def test_import_vr_media_group_marks_4k_from_merged_video_info(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "SIVR-010-part1.mp4").write_bytes(b"first")
    (source_dir / "SIVR-010-part2.mp4").write_bytes(b"second")
    library_root = tmp_path / "library"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"SIVR-010": _build_detail("SIVR-010")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
        media_metadata_probe_service=FakeMediaMetadataProbeService(
            resolution="3840x2160",
            duration_seconds=3600,
            video_info=_build_video_info(3840, 2160),
        ),
    )

    def _fake_merge(files, target_path: Path) -> None:
        target_path.write_bytes(b"merged-video")

    monkeypatch.setattr(service, "_merge_media_files", _fake_merge)

    service.import_from_source(str(source_dir), library.id)

    media = Media.get()
    assert media.storage_mode == "concat"
    assert media.special_tags == "4K VR"


def test_import_media_revive_invalid_media_keeps_existing_video_info_when_probe_is_empty(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-192.mp4").write_bytes(b"x" * 10)
    old_path = tmp_path / "old-library" / "ABP-192" / "video.mp4"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    movie = Movie.create(javdb_id="javdb-ABP-192", movie_number="ABP-192", title="old-title")
    existing_video_info = {
        "container": {"format_name": "mp4", "duration_seconds": 120},
        "video": {"codec_name": "h264", "profile": "Main", "width": 3840, "height": 2160},
        "audio": None,
        "subtitles": [],
    }
    invalid_media = Media.create(
        movie=movie,
        library=library,
        path=str(old_path),
        storage_mode="copy",
        content_fingerprint="stale",
        file_size_bytes=3,
        resolution="1280x720",
        duration_seconds=120,
        video_info=existing_video_info,
        special_tags="4K",
        valid=False,
    )

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-192": _build_detail("ABP-192")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
        media_metadata_probe_service=FakeMediaMetadataProbeService(
            resolution=None,
            duration_seconds=0,
            video_info=None,
        ),
    )

    expected_fingerprint = service._build_content_fingerprint(source_dir / "ABP-192.mp4", "ABP-192")
    invalid_media.content_fingerprint = expected_fingerprint
    invalid_media.save(only=[Media.content_fingerprint])

    service.import_from_source(str(source_dir), library.id)
    revived_media = Media.get_by_id(invalid_media.id)

    assert revived_media.video_info == existing_video_info
    assert revived_media.special_tags == "4K"


def test_content_fingerprint_changes_when_movie_number_changes(import_tables, tmp_path):
    file_path = tmp_path / "ABP-123.mp4"
    file_path.write_bytes(b"x" * 10)
    service = MediaImportService(
        provider=FakeJavdbProvider({}),
        image_downloader=_fake_downloader,
    )

    first = service._build_content_fingerprint(file_path, "ABP-123")
    second = service._build_content_fingerprint(file_path, "SSIS-001")

    assert first != second


def test_build_content_fingerprint_reads_full_file_for_small_inputs(import_tables, tmp_path, monkeypatch):
    file_path = tmp_path / "ABP-123.mp4"
    file_path.write_bytes(b"abcdefghijklmnopqrstuvwxyz")
    service = MediaImportService(
        provider=FakeJavdbProvider({}),
        image_downloader=_fake_downloader,
    )
    captured_ranges: List[tuple[int, int]] = []
    original = service._update_hash_with_range

    monkeypatch.setattr(MediaImportService, "FULL_HASH_THRESHOLD_BYTES", 100)

    def _record_range(hasher, target_path: Path, start: int, end: int) -> None:
        captured_ranges.append((start, end))
        original(hasher, target_path, start, end)

    monkeypatch.setattr(service, "_update_hash_with_range", _record_range)

    service._build_content_fingerprint(file_path, "ABP-123")

    assert captured_ranges == [(0, file_path.stat().st_size)]


def test_build_content_fingerprint_uses_sparse_ranges_for_large_inputs(import_tables, tmp_path, monkeypatch):
    file_path = tmp_path / "ABP-123.mp4"
    file_path.write_bytes(bytes(range(64)))
    service = MediaImportService(
        provider=FakeJavdbProvider({}),
        image_downloader=_fake_downloader,
    )
    captured_ranges: List[tuple[int, int]] = []
    original = service._update_hash_with_range

    monkeypatch.setattr(MediaImportService, "FULL_HASH_THRESHOLD_BYTES", 16)
    monkeypatch.setattr(MediaImportService, "SAMPLE_WINDOW_BYTES", 4)
    monkeypatch.setattr(MediaImportService, "INTERIOR_SAMPLE_COUNT", 6)

    def _record_range(hasher, target_path: Path, start: int, end: int) -> None:
        captured_ranges.append((start, end))
        original(hasher, target_path, start, end)

    monkeypatch.setattr(service, "_update_hash_with_range", _record_range)

    service._build_content_fingerprint(file_path, "ABP-123")

    assert captured_ranges == [
        (0, 4),
        (7, 11),
        (16, 20),
        (25, 29),
        (34, 38),
        (43, 47),
        (52, 56),
        (60, 64),
    ]


def test_import_media_revives_invalid_media_and_preserves_thumbnail(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    old_path = tmp_path / "old-library" / "ABP-123" / "video.mp4"
    old_path.parent.mkdir(parents=True, exist_ok=True)
    old_path.write_bytes(b"old")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    movie = Movie.create(javdb_id="javdb-ABP-123", movie_number="ABP-123", title="old-title")
    image = Image.create(origin="thumb.jpg", small="thumb.jpg", medium="thumb.jpg", large="thumb.jpg")
    invalid_media = Media.create(
        movie=movie,
        library=library,
        path=str(old_path),
        storage_mode="copy",
        content_fingerprint="stale",
        file_size_bytes=3,
        valid=False,
    )
    ResourceTaskState.create(
        task_key=MediaThumbnailService.TASK_KEY,
        resource_type="media",
        resource_id=invalid_media.id,
        state="failed",
        attempt_count=2,
        last_attempted_at=datetime(2026, 4, 17, 10, 0, 0),
        last_succeeded_at=datetime(2026, 4, 16, 10, 0, 0),
        last_error="thumbnail_generation_empty",
        extra={"terminal": True},
    )
    thumbnail = MediaThumbnail.create(media=invalid_media, image=image, offset=10)

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    expected_fingerprint = service._build_content_fingerprint(source_dir / "ABP-123.mp4", "ABP-123")
    invalid_media.content_fingerprint = expected_fingerprint
    invalid_media.save(only=[Media.content_fingerprint])

    job = service.import_from_source(str(source_dir), library.id)

    revived_media = Media.get_by_id(invalid_media.id)
    assert job.state == "completed"
    assert job.imported_count == 1
    assert Media.select().count() == 1
    assert revived_media.valid is True
    assert revived_media.path != str(old_path)
    assert revived_media.path.endswith("/ABP-123/1730000000000/ABP-123.mp4")
    assert revived_media.content_fingerprint == expected_fingerprint
    assert revived_media.file_size_bytes == 10
    assert revived_media.special_tags == "普通"
    thumbnail_task_state = ResourceTaskStateService.get_state(
        MediaThumbnailService.TASK_KEY,
        revived_media.id,
    )
    assert thumbnail_task_state is not None
    assert thumbnail_task_state.state == "pending"
    assert thumbnail_task_state.attempt_count == 0
    assert thumbnail_task_state.last_error is None
    assert thumbnail_task_state.last_attempted_at is None
    assert thumbnail_task_state.last_succeeded_at is None
    assert thumbnail_task_state.extra is None
    assert MediaThumbnail.get_by_id(thumbnail.id).media_id == revived_media.id


def test_import_media_delegates_catalog_upsert_to_catalog_import_service(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    spy_catalog_service = SpyCatalogImportService()
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
        catalog_import_service=spy_catalog_service,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 1
    assert spy_catalog_service.called_movie_numbers == ["ABP-123"]
    assert spy_catalog_service.force_subscribed_args == [True]


def test_import_media_marks_existing_unsubscribed_movie_as_subscribed(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        is_subscribed=False,
        subscribed_at=None,
    )

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at is not None


def test_import_media_keeps_existing_subscribed_at_for_already_subscribed_movie(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))
    original_timestamp = datetime(2026, 3, 8, 9, 0, 0)
    Movie.create(
        javdb_id="javdb-ABP-123",
        movie_number="ABP-123",
        title="old-title",
        is_subscribed=True,
        subscribed_at=original_timestamp,
    )

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    movie = Movie.get(Movie.movie_number == "ABP-123")
    assert movie.is_subscribed is True
    assert movie.subscribed_at == original_timestamp


def test_import_media_fetches_metadata_with_thread_pool(import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    (source_dir / "ABP-124.mp4").write_bytes(b"y" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.metadata, "import_metadata_max_workers", 2)

    ready = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    started_movie_numbers: List[str] = []

    class BlockingProvider(FakeJavdbProvider):
        def get_movie_by_number(self, movie_number: str) -> JavdbMovieDetailResource:
            with lock:
                started_movie_numbers.append(movie_number)
                if len(started_movie_numbers) == 2:
                    ready.set()
            release.wait(timeout=2)
            return super().get_movie_by_number(movie_number)

    provider = BlockingProvider(
        {
            "ABP-123": _build_detail("ABP-123"),
            "ABP-124": _build_detail("ABP-124"),
        }
    )
    service = MediaImportService(
        provider=provider,
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    result: Dict[str, ImportJob] = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("job", service.import_from_source(str(source_dir), library.id))
    )
    worker.start()

    assert ready.wait(timeout=1), "expected metadata workers to overlap"
    with lock:
        assert set(started_movie_numbers) == {"ABP-123", "ABP-124"}

    release.set()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert result["job"].state == "completed"
    assert result["job"].imported_count == 2


def test_import_media_keeps_file_import_order_when_metadata_finishes_out_of_order(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    (source_dir / "ABP-124.mp4").write_bytes(b"y" * 10)
    library_root = tmp_path / "library"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.metadata, "import_metadata_max_workers", 2)

    class SlowFirstProvider(FakeJavdbProvider):
        def get_movie_by_number(self, movie_number: str) -> JavdbMovieDetailResource:
            if movie_number == "ABP-123":
                time.sleep(0.2)
            return super().get_movie_by_number(movie_number)

    service = MediaImportService(
        provider=SlowFirstProvider(
            {
                "ABP-123": _build_detail("ABP-123"),
                "ABP-124": _build_detail("ABP-124"),
            }
        ),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    imported_movie_numbers: List[str] = []

    def _record_import(file_path: Path, library: MediaLibrary, movie_number: str):
        imported_movie_numbers.append(movie_number)
        target_directory = Path(library.root_path) / movie_number / str(service.now_ms())
        target_directory.mkdir(parents=True, exist_ok=True)
        target_path = target_directory / f"{movie_number}{file_path.suffix.lower()}"
        target_path.write_bytes(file_path.read_bytes())
        return "copy", target_path

    monkeypatch.setattr(service, "_import_single_media_file", _record_import)

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert imported_movie_numbers == ["ABP-123", "ABP-124"]


def test_import_media_parallel_metadata_failure_still_imports_other_movies(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123.mp4").write_bytes(b"x" * 10)
    (source_dir / "ABP-124.mp4").write_bytes(b"y" * 10)
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))
    monkeypatch.setattr(settings.metadata, "import_metadata_max_workers", 2)

    service = MediaImportService(
        provider=FakeJavdbProvider(
            {"ABP-123": _build_detail("ABP-123")},
            failures={"ABP-124": MetadataNotFoundError("movie", "ABP-124")},
        ),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "failed"
    assert job.imported_count == 1
    assert job.failed_count == 1
    assert Media.select().count() == 1
    assert any(item["reason"] == "metadata_fetch_failed" for item in _read_failed_files(job))


def test_import_media_merges_multi_file_vr_into_single_media(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    first = source_dir / "SIVR-001-part2.mp4"
    second = source_dir / "SIVR-001-part1.mp4"
    first.write_bytes(b"second-fragment")
    second.write_bytes(b"first-fragment")
    (source_dir / "SIVR-001-part1.srt").write_text("subtitle", encoding="utf-8")
    library_root = tmp_path / "library"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"SIVR-001": _build_detail("SIVR-001")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    merged_inputs: List[str] = []

    def _fake_merge(files, target_path: Path) -> None:
        merged_inputs.extend(file.path.name for file in files)
        target_path.write_bytes(b"merged-video")

    monkeypatch.setattr(service, "_merge_media_files", _fake_merge)

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 1
    assert job.skipped_count == 0
    assert job.failed_count == 0
    assert merged_inputs == ["SIVR-001-part1.mp4", "SIVR-001-part2.mp4"]

    media = Media.get()
    assert media.storage_mode == "concat"
    assert media.special_tags == "中字 VR"
    assert media.file_size_bytes == len(b"merged-video")
    assert Path(media.path).name == "SIVR-001.mp4"
    assert (library_root / "SIVR-001" / "1730000000000" / "SIVR-001.srt").read_text(encoding="utf-8") == "subtitle"


def test_import_media_keeps_non_vr_multi_file_as_separate_media(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "ABP-123-cd2.mp4").write_bytes(b"second")
    (source_dir / "ABP-123-cd1.mp4").write_bytes(b"first")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"ABP-123": _build_detail("ABP-123")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 2
    assert Media.select().count() == 2
    assert {media.storage_mode for media in Media.select()} <= {"copy", "hardlink"}


def test_group_content_fingerprint_changes_when_fragment_order_changes(import_tables, tmp_path):
    first = tmp_path / "SIVR-001-part1.mp4"
    second = tmp_path / "SIVR-001-part2.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    service = MediaImportService(
        provider=FakeJavdbProvider({}),
        image_downloader=_fake_downloader,
    )

    first_fingerprint = service._build_content_fingerprint(first, "SIVR-001")
    second_fingerprint = service._build_content_fingerprint(second, "SIVR-001")

    ordered = service._build_group_content_fingerprint([first_fingerprint, second_fingerprint])
    reversed_value = service._build_group_content_fingerprint([second_fingerprint, first_fingerprint])

    assert ordered != reversed_value


def test_import_media_skips_duplicate_fragments_inside_vr_group(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "SIVR-001-part1.mp4").write_bytes(b"same-fragment")
    (source_dir / "SIVR-001-part2.mp4").write_bytes(b"same-fragment")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"SIVR-001": _build_detail("SIVR-001")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    merged_inputs: List[str] = []

    def _fake_merge(files, target_path: Path) -> None:
        merged_inputs.extend(file.path.name for file in files)
        target_path.write_bytes(b"merged-video")

    monkeypatch.setattr(service, "_merge_media_files", _fake_merge)

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.imported_count == 1
    assert job.skipped_count == 1
    assert merged_inputs == ["SIVR-001-part1.mp4"]


def test_import_media_skips_multiple_subtitles_for_merged_vr_group(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "SIVR-001-part1.mp4").write_bytes(b"first")
    (source_dir / "SIVR-001-part2.mp4").write_bytes(b"second")
    (source_dir / "SIVR-001-part1.srt").write_text("subtitle-1", encoding="utf-8")
    (source_dir / "SIVR-001-part2.srt").write_text("subtitle-2", encoding="utf-8")
    library_root = tmp_path / "library"
    library = MediaLibrary.create(name="Main", root_path=str(library_root))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"SIVR-001": _build_detail("SIVR-001")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    monkeypatch.setattr(service, "_merge_media_files", lambda files, target_path: target_path.write_bytes(b"merged"))

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "completed"
    assert job.failed_count == 0
    assert list(library_root.rglob("*.srt")) == []
    assert any(item["reason"] == "merge_subtitle_skipped_multiple_sidecars" for item in _read_failed_files(job))


def test_import_media_marks_group_failed_when_vr_merge_raises(
    import_tables, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = tmp_path / "source"
    source_dir.mkdir(parents=True)
    (source_dir / "SIVR-001-part1.mp4").write_bytes(b"first")
    (source_dir / "SIVR-001-part2.mp4").write_bytes(b"second")
    library = MediaLibrary.create(name="Main", root_path=str(tmp_path / "library"))

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 1)
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "images"))

    service = MediaImportService(
        provider=FakeJavdbProvider({"SIVR-001": _build_detail("SIVR-001")}),
        image_downloader=_fake_downloader,
        now_ms=lambda: 1730000000000,
    )

    def _raise_merge(files, target_path: Path) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "_merge_media_files", _raise_merge)

    job = service.import_from_source(str(source_dir), library.id)

    assert job.state == "failed"
    assert job.imported_count == 0
    assert job.failed_count == 1
    assert Media.select().count() == 0
    assert any(item["reason"] == "vr_media_merge_failed" for item in _read_failed_files(job))
