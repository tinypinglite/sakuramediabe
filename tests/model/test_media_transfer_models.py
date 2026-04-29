import pytest
from peewee import IntegrityError

from src.model import (
    BackgroundTaskRun,
    DownloadClient,
    DownloadTask,
    Image,
    ImportJob,
    Indexer,
    Media,
    MediaLibrary,
    Movie,
    MovieSeries,
)


@pytest.fixture()
def media_transfer_tables(test_db):
    models = [
        Image,
        MovieSeries,
        Movie,
        MediaLibrary,
        DownloadClient,
        Indexer,
        DownloadTask,
        BackgroundTaskRun,
        ImportJob,
        Media,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def test_media_library_name_and_root_path_must_be_unique(media_transfer_tables):
    MediaLibrary.create(name="A", root_path="/media/a")

    with pytest.raises(IntegrityError):
        MediaLibrary.create(name="A", root_path="/media/b")

    with pytest.raises(IntegrityError):
        MediaLibrary.create(name="B", root_path="/media/a")


def test_download_task_requires_unique_info_hash_per_client(media_transfer_tables):
    library = MediaLibrary.create(name="A", root_path="/library/a")
    client_a = DownloadClient.create(
        name="client-a",
        base_url="https://qb-a.example.com:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )
    client_b = DownloadClient.create(
        name="client-b",
        base_url="https://qb-b.example.com:8080",
        username="bob",
        password="secret",
        client_save_path="/downloads/b",
        local_root_path="/mnt/downloads/b",
        media_library=library,
    )

    DownloadTask.create(
        client=client_a,
        name="ABC-001",
        info_hash="hash-1",
        save_path="/downloads/a/ABC-001",
    )

    with pytest.raises(IntegrityError):
        DownloadTask.create(
            client=client_a,
            name="ABC-001 duplicate",
            info_hash="hash-1",
            save_path="/downloads/a/ABC-001-duplicate",
        )

    task = DownloadTask.create(
        client=client_b,
        name="ABC-001 mirrored",
        info_hash="hash-1",
        save_path="/downloads/b/ABC-001",
    )

    assert task.client_id == client_b.id


def test_import_job_can_link_download_task_and_store_failed_files(media_transfer_tables):
    library = MediaLibrary.create(name="A", root_path="/library/a")
    client = DownloadClient.create(
        name="client-a",
        base_url="https://qb-a.example.com:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )
    task = DownloadTask.create(
        client=client,
        name="ABC-001",
        info_hash="hash-1",
        save_path="/downloads/a/ABC-001",
        progress=1,
        download_state="completed",
    )

    job = ImportJob.create(
        source_path="/downloads/a/ABC-001",
        library=library,
        download_task=task,
        state="failed",
        failed_count=1,
        failed_files='[{"path":"/downloads/a/ABC-001/video.mkv","reason":"parse failed"}]',
    )

    assert job.download_task_id == task.id
    assert "parse failed" in job.failed_files


def test_indexer_name_must_be_unique_and_belongs_to_download_client(media_transfer_tables):
    library = MediaLibrary.create(name="A", root_path="/library/a")
    client = DownloadClient.create(
        name="client-a",
        base_url="https://qb-a.example.com:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )

    Indexer.create(
        name="mteam",
        url="http://127.0.0.1:9117/api/v2.0/indexers/mteam/results/torznab/",
        kind="pt",
        download_client=client,
    )

    with pytest.raises(IntegrityError):
        Indexer.create(
            name="mteam",
            url="https://example.com/api/v2.0/indexers/other/results/torznab/",
            kind="bt",
            download_client=client,
        )


def test_media_keeps_absolute_path_and_library(media_transfer_tables):
    library = MediaLibrary.create(name="A", root_path="/library/a")
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")

    media = Media.create(
        movie=movie,
        path="/library/a/ABC-001/video.mkv",
        library=library,
        storage_mode="hardlink",
    )

    assert media.path == "/library/a/ABC-001/video.mkv"
    assert media.library_id == library.id
    assert media.storage_mode == "hardlink"


def test_media_special_tags_defaults_and_supports_space_separated_values(media_transfer_tables):
    library = MediaLibrary.create(name="A", root_path="/library/a")
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")

    default_media = Media.create(
        movie=movie,
        path="/library/a/ABC-001/default.mkv",
        library=library,
        storage_mode="copy",
    )
    tagged_media = Media.create(
        movie=movie,
        path="/library/a/ABC-001/tagged.mkv",
        library=library,
        storage_mode="copy",
        special_tags="4K 无码",
    )

    assert default_media.special_tags == "普通"
    assert tagged_media.special_tags == "4K 无码"


def test_media_can_store_content_fingerprint(media_transfer_tables):
    library = MediaLibrary.create(name="A", root_path="/library/a")
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")

    media = Media.create(
        movie=movie,
        path="/library/a/ABC-001/video.mkv",
        library=library,
        storage_mode="copy",
        content_fingerprint="fingerprint-1",
    )

    assert media.content_fingerprint == "fingerprint-1"
