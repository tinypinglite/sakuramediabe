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
    VideoItem,
)


@pytest.fixture()
def media_transfer_tables(test_db):
    models = [
        Image,
        MovieSeries,
        Movie,
        VideoItem,
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


def test_media_library_name_must_be_unique(media_transfer_tables):
    # name 唯一走 DB 约束；root_path 唯一下沉到 service 层（backend_config 是 JSON，
    # DB 层不管），见 tests/service/test_media_library_service.py::test_create_library_rejects_root_path_conflict。
    MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/media/a"})

    with pytest.raises(IntegrityError):
        MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/media/b"})


def test_download_task_requires_unique_info_hash_per_client(media_transfer_tables):
    library = MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/library/a"})
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
    library = MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/library/a"})
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
    library = MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/library/a"})
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
    library = MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/library/a"})
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
    library = MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/library/a"})
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
    library = MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/library/a"})
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")

    media = Media.create(
        movie=movie,
        path="/library/a/ABC-001/video.mkv",
        library=library,
        storage_mode="copy",
        content_fingerprint="fingerprint-1",
    )

    assert media.content_fingerprint == "fingerprint-1"


def test_media_can_store_backend_locator_without_path(media_transfer_tables):
    """cloud115 / 未来 smb 等云盘 backend 场景：path 留空，用 backend_locator 定位。"""
    library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={"cookies": "UID=1_A1_1", "root_cid": "3000000", "app": "alipaymini"},
    )
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")

    media = Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "1234", "pickcode": "abcdef", "name": "ABC-001.mp4"},
    )

    assert media.path is None
    assert media.backend_locator == {
        "fid": "1234",
        "pickcode": "abcdef",
        "name": "ABC-001.mp4",
    }


def test_media_rejects_when_path_and_backend_locator_both_empty(media_transfer_tables):
    """存储定位不变量：path 和 backend_locator 至少一个非空，都空要抛 ValueError。"""
    library = MediaLibrary.create(name="A", backend="local", backend_config={"root_path": "/library/a"})
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")

    with pytest.raises(ValueError, match="path or backend_locator"):
        Media.create(
            movie=movie,
            library=library,
        )


def test_media_rejects_duplicate_backend_locator_in_same_library(media_transfer_tables):
    """(library, backend_locator) 复合唯一：同库同 locator 重复登记要被 DB 拒绝。"""
    library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={"cookies": "UID=1_A1_1", "root_cid": "3000000", "app": "alipaymini"},
    )
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")
    locator = {"fid": "1234", "pickcode": "abcdef", "name": "ABC-001.mp4"}

    Media.create(movie=movie, library=library, backend_locator=locator)

    with pytest.raises(IntegrityError):
        Media.create(movie=movie, library=library, backend_locator=locator)


def test_media_allows_same_backend_locator_across_libraries(media_transfer_tables):
    """不同库（不同 115 账号）允许出现相同 locator 值，唯一性限定在库内。"""
    library_a = MediaLibrary.create(
        name="cloud-a",
        backend="cloud115",
        backend_config={"cookies": "UID=1_A1_1", "root_cid": "3000000", "app": "alipaymini"},
    )
    library_b = MediaLibrary.create(
        name="cloud-b",
        backend="cloud115",
        backend_config={"cookies": "UID=2_A1_1", "root_cid": "4000000", "app": "alipaymini"},
    )
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")
    locator = {"fid": "1234", "pickcode": "abcdef", "name": "ABC-001.mp4"}

    Media.create(movie=movie, library=library_a, backend_locator=locator)
    Media.create(movie=movie, library=library_b, backend_locator=locator)

    assert Media.select().count() == 2


def test_media_allows_multiple_cloud_rows_without_path(media_transfer_tables):
    """path 变可空后，多条 cloud115 Media（path 都是 NULL）不会撞 unique 约束
    （PostgreSQL: NULL != NULL，不参与 unique）。"""
    library = MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={"cookies": "UID=1_A1_1", "root_cid": "3000000", "app": "alipaymini"},
    )
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")

    Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "1", "pickcode": "pc-1", "name": "a.mp4"},
    )
    Media.create(
        movie=movie,
        library=library,
        backend_locator={"fid": "2", "pickcode": "pc-2", "name": "b.mp4"},
    )

    assert Media.select().where(Media.path.is_null()).count() == 2
