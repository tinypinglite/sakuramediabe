import pytest

from src.api.exception.errors import ApiError
from src.model import BackgroundTaskRun, DownloadClient, DownloadTask, Image, ImportJob, Media, MediaLibrary, Movie, MovieSeries
from src.schema.playback.media_libraries import (
    MediaLibraryCreateRequest,
    MediaLibraryUpdateRequest,
)
from src.service.playback import MediaLibraryService


@pytest.fixture()
def media_library_tables(test_db):
    models = [Image, MovieSeries, Movie, MediaLibrary, DownloadClient, DownloadTask, BackgroundTaskRun, ImportJob, Media]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def test_list_libraries_returns_created_at_desc_then_id_desc(media_library_tables):
    first = MediaLibrary.create(name="A", root_path="/library/a")
    second = MediaLibrary.create(name="B", root_path="/library/b")

    items = MediaLibraryService.list_libraries()

    assert [item.id for item in items] == [second.id, first.id]


def test_create_library_strips_and_persists_normalized_fields(media_library_tables):
    resource = MediaLibraryService.create_library(
        MediaLibraryCreateRequest(name="  Main  ", root_path="  /library/main  ")
    )

    assert resource.name == "Main"
    assert resource.root_path == "/library/main"
    assert MediaLibrary.get_by_id(resource.id).name == "Main"


def test_create_library_rejects_empty_name(media_library_tables):
    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(name="   ", root_path="/library/main")
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_media_library_name"


def test_create_library_rejects_invalid_root_path(media_library_tables):
    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(name="Main", root_path="relative/path")
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "invalid_media_library_root_path"


def test_create_library_rejects_name_conflict(media_library_tables):
    MediaLibrary.create(name="Main", root_path="/library/main")

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(name="Main", root_path="/library/other")
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_name_conflict"


def test_create_library_rejects_root_path_conflict(media_library_tables):
    MediaLibrary.create(name="Main", root_path="/library/main")

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.create_library(
            MediaLibraryCreateRequest(name="Other", root_path="/library/main")
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_root_path_conflict"


def test_update_library_rejects_empty_payload(media_library_tables):
    library = MediaLibrary.create(name="Main", root_path="/library/main")

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(library.id, MediaLibraryUpdateRequest())

    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "empty_media_library_update"


def test_update_library_rejects_missing_library(media_library_tables):
    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(
            999,
            MediaLibraryUpdateRequest(name="Updated"),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "media_library_not_found"


def test_update_library_rejects_name_conflict(media_library_tables):
    library = MediaLibrary.create(name="Main", root_path="/library/main")
    MediaLibrary.create(name="Other", root_path="/library/other")

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(
            library.id,
            MediaLibraryUpdateRequest(name="Other"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_name_conflict"


def test_update_library_rejects_root_path_conflict(media_library_tables):
    library = MediaLibrary.create(name="Main", root_path="/library/main")
    MediaLibrary.create(name="Other", root_path="/library/other")

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.update_library(
            library.id,
            MediaLibraryUpdateRequest(root_path="/library/other"),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_root_path_conflict"


def test_delete_library_rejects_when_media_exists(media_library_tables):
    library = MediaLibrary.create(name="Main", root_path="/library/main")
    movie = Movie.create(javdb_id="MovieA1", movie_number="ABC-001", title="ABC-001")
    Media.create(movie=movie, path="/library/main/video.mp4", library=library)

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.delete_library(library.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_in_use"


def test_delete_library_rejects_when_download_client_exists(media_library_tables):
    library = MediaLibrary.create(name="Main", root_path="/library/main")
    DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.delete_library(library.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_in_use"


def test_delete_library_rejects_when_import_job_exists(media_library_tables):
    library = MediaLibrary.create(name="Main", root_path="/library/main")
    ImportJob.create(source_path="/downloads/a", library=library)

    with pytest.raises(ApiError) as exc_info:
        MediaLibraryService.delete_library(library.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "media_library_in_use"
