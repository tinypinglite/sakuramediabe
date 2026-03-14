from pathlib import Path

import pytest

from src.api.exception.errors import ApiError
from src.model import DownloadClient, DownloadTask, Image, ImportJob, Indexer, MediaLibrary
from src.schema.transfers.downloads import (
    DownloadClientCreateRequest,
    DownloadClientUpdateRequest,
    DownloadRequestCreateRequest,
)
from src.service.transfers.download_client_service import DownloadClientService
from src.service.transfers.download_request_service import DownloadRequestService
from src.service.transfers.download_search_service import DownloadSearchService
from src.service.transfers.download_sync_service import DownloadSyncService
from src.service.transfers.download_task_service import DownloadTaskService
from src.service.transfers.jackett_client import JackettClient


@pytest.fixture()
def download_tables(test_db):
    models = [Image, MediaLibrary, DownloadClient, Indexer, DownloadTask, ImportJob]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


def _create_library(name: str = "Main", root_path: str = "/library/main") -> MediaLibrary:
    return MediaLibrary.create(name=name, root_path=root_path)


def _create_client(
    library: MediaLibrary,
    *,
    name: str = "client-a",
    password: str = "secret",
) -> DownloadClient:
    return DownloadClient.create(
        name=name,
        base_url="http://localhost:8080",
        username="alice",
        password=password,
        client_save_path="/downloads/a",
        local_root_path="/mnt/downloads/a",
        media_library=library,
    )


def test_download_client_crud_services(download_tables):
    library = _create_library()
    other_library = _create_library(name="Archive", root_path="/library/archive")

    created = DownloadClientService.create_client(
        DownloadClientCreateRequest(
            name=" client-a ",
            base_url=" http://localhost:8080 ",
            username=" alice ",
            password=" secret ",
            client_save_path=" /downloads/a ",
            local_root_path=" /mnt/downloads/a ",
            media_library_id=library.id,
        )
    )
    updated = DownloadClientService.update_client(
        created.id,
        DownloadClientUpdateRequest(
            name="client-renamed",
            base_url="https://qb.example.com",
            username="bob",
            client_save_path="/downloads/b",
            local_root_path="/mnt/downloads/b",
            media_library_id=other_library.id,
        ),
    )
    listed = DownloadClientService.list_clients()

    assert created.client_save_path == "/downloads/a"
    assert created.local_root_path == "/mnt/downloads/a"
    assert updated.name == "client-renamed"
    assert updated.client_save_path == "/downloads/b"
    assert updated.local_root_path == "/mnt/downloads/b"
    assert updated.media_library_id == other_library.id
    assert [item.id for item in listed] == [created.id]

    DownloadClientService.delete_client(created.id)
    assert DownloadClient.get_or_none(DownloadClient.id == created.id) is None


def test_download_client_service_reports_validation_errors(download_tables):
    library = _create_library()
    _create_client(library, name="client-a")

    with pytest.raises(ApiError) as conflict:
        DownloadClientService.create_client(
            DownloadClientCreateRequest(
                name="client-a",
                base_url="http://localhost:8080",
                username="alice",
                password="secret",
                client_save_path="/downloads/a",
                local_root_path="/mnt/downloads/a",
                media_library_id=library.id,
            )
        )
    assert conflict.value.code == "download_client_name_conflict"

    with pytest.raises(ApiError) as invalid_path:
        DownloadClientService.create_client(
            DownloadClientCreateRequest(
                name="client-b",
                base_url="http://localhost:8080",
                username="alice",
                password="secret",
                client_save_path="downloads/a",
                local_root_path="/mnt/downloads/a",
                media_library_id=library.id,
            )
        )
    assert invalid_path.value.code == "invalid_download_client_client_save_path"


def test_download_task_list_and_delete(download_tables):
    library = _create_library()
    client = _create_client(library)
    first = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="task-1",
        info_hash="hash-1",
        save_path="/mnt/downloads/a/ABC-001",
        progress=0.5,
        download_state="downloading",
        import_status="pending",
    )
    DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="task-2",
        info_hash="hash-2",
        save_path="/mnt/downloads/a/ABC-001-file",
        progress=1.0,
        download_state="completed",
        import_status="completed",
    )

    paged = DownloadTaskService.list_tasks(
        page=1,
        page_size=10,
        client_id=client.id,
        download_state="completed",
        import_status="completed",
        movie_number="abc-001",
        query="hash-2",
        sort="created_at:asc",
    )
    DownloadTaskService.delete_tasks(str(first.id))

    assert paged.total == 1
    assert paged.items[0].info_hash == "hash-2"
    assert DownloadTask.get_or_none(DownloadTask.id == first.id) is None


def test_jackett_client_parses_and_sorts_candidates(download_tables):
    class FakeResponse:
        text = """
        <rss>
          <channel>
            <item>
              <title>ABC-001 4K</title>
              <description>中字</description>
              <size>12884901888</size>
              <link>https://indexer.example/download/1</link>
              <torznab:attr name="seeders" value="18"/>
              <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:HASHA"/>
            </item>
            <item>
              <title>ABC-001 normal</title>
              <description></description>
              <size>3221225472</size>
              <link>https://indexer.example/download/2</link>
              <torznab:attr name="seeders" value="3"/>
            </item>
          </channel>
        </rss>
        """

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def get(self, url, params):
            return FakeResponse()

    library = _create_library()
    download_client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=download_client,
    )
    client = JackettClient(api_key="secret", client=FakeHttpClient())
    results = client.search("ABC-001")

    assert len(results) == 2
    assert results[0].seeders == 18
    assert results[0].tags == ["中字", "4K"]
    assert results[0].resolved_client_id == download_client.id
    assert results[0].resolved_client_name == download_client.name
    assert results[0].indexer_name == "mteam"


def test_jackett_client_keeps_local_indexer_name_when_jackettindexer_is_dict(download_tables):
    class FakeResponse:
        text = """
        <rss>
          <channel>
            <title>M-Team Display</title>
            <item>
              <title>ABC-001 4K</title>
              <description>中字</description>
              <size>12884901888</size>
              <link>https://indexer.example/download/1</link>
              <jackettindexer id="mteam">M-Team Display</jackettindexer>
              <torznab:attr name="seeders" value="18"/>
              <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:HASHA"/>
            </item>
          </channel>
        </rss>
        """

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def get(self, url, params):
            return FakeResponse()

    library = _create_library()
    download_client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=download_client,
    )

    results = JackettClient(api_key="secret", client=FakeHttpClient()).search("ABC-001")

    assert len(results) == 1
    assert results[0].indexer_name == "mteam"
    assert results[0].seeders == 18


def test_jackett_client_keeps_local_indexer_name_when_indexer_is_dict(download_tables):
    class FakeResponse:
        text = """
        <rss>
          <channel>
            <title>M-Team Display</title>
            <item>
              <title>ABC-001 normal</title>
              <description></description>
              <size>3221225472</size>
              <link>https://indexer.example/download/2</link>
              <indexer id="mteam">M-Team Display</indexer>
              <torznab:attr name="seeders" value="3"/>
            </item>
          </channel>
        </rss>
        """

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def get(self, url, params):
            return FakeResponse()

    library = _create_library()
    download_client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=download_client,
    )

    results = JackettClient(api_key="secret", client=FakeHttpClient()).search("ABC-001")

    assert len(results) == 1
    assert results[0].indexer_name == "mteam"
    assert results[0].torrent_url == "https://indexer.example/download/2"


def test_jackett_client_handles_missing_item_indexer_fields_with_channel_title_fallback(download_tables):
    class FakeResponse:
        text = """
        <rss>
          <channel>
            <title>M-Team Display</title>
            <item>
              <title>ABC-001 normal</title>
              <description>无码</description>
              <size>3221225472</size>
              <link>https://indexer.example/download/2</link>
              <torznab:attr name="seeders" value="3"/>
            </item>
          </channel>
        </rss>
        """

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def get(self, url, params):
            return FakeResponse()

    library = _create_library()
    download_client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=download_client,
    )

    results = JackettClient(api_key="secret", client=FakeHttpClient()).search("ABC-001")

    assert len(results) == 1
    assert results[0].indexer_name == "mteam"
    assert results[0].title == "ABC-001 normal 无码"


def test_download_search_service_rejects_invalid_indexer_kind(download_tables):
    with pytest.raises(ApiError) as exc_info:
        DownloadSearchService().search_candidates(movie_number="ABC-001", indexer_kind="ftp")
    assert exc_info.value.code == "invalid_download_candidate_indexer_kind"


def test_download_request_service_adds_task_and_passes_client_save_path(download_tables):
    library = _create_library()
    client = _create_client(library)
    called = {}

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            called["client_id"] = download_client.id
            return cls()

        def add_candidate(self, **kwargs):
            called.update(kwargs)
            return {
                "info_hash": "abcdef123456",
                "name": "ABC-001",
                "progress": 0.0,
                "state": "queuedDL",
                "save_path": "/downloads/a/ABC-001",
            }

    service = DownloadRequestService(qbittorrent_client_cls=FakeQBittorrentClient)
    result = service.create_request(
        DownloadRequestCreateRequest.model_validate(
            {
                "client_id": client.id,
                "movie_number": "ABC-001",
                "candidate": {
                    "source": "jackett",
                    "indexer_name": "mteam",
                    "indexer_kind": "pt",
                    "title": "ABC-001",
                    "size_bytes": 123,
                    "seeders": 5,
                    "magnet_url": "magnet:?xt=urn:btih:ABCDEF123456",
                    "torrent_url": "",
                    "tags": [],
                },
            }
        )
    )

    stored = DownloadTask.get()
    assert called["save_path"] == "/downloads/a"
    assert called["client_id"] == client.id
    assert result.created is True
    assert stored.save_path == "/mnt/downloads/a/ABC-001"
    assert stored.info_hash == "abcdef123456"
    assert stored.movie == "ABC-001"


def test_download_request_service_resolves_client_from_indexer_when_client_id_missing(download_tables):
    library = _create_library()
    client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=client,
    )
    called = {}

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            called["client_id"] = download_client.id
            return cls()

        def add_candidate(self, **kwargs):
            called.update(kwargs)
            return {
                "info_hash": "abcdef123456",
                "name": "ABC-001",
                "progress": 0.0,
                "state": "queuedDL",
                "save_path": "/downloads/a/ABC-001",
            }

    service = DownloadRequestService(qbittorrent_client_cls=FakeQBittorrentClient)
    result = service.create_request(
        DownloadRequestCreateRequest.model_validate(
            {
                "movie_number": "ABC-001",
                "candidate": {
                    "source": "jackett",
                    "indexer_name": "mteam",
                    "indexer_kind": "pt",
                    "title": "ABC-001",
                    "size_bytes": 123,
                    "seeders": 5,
                    "magnet_url": "magnet:?xt=urn:btih:ABCDEF123456",
                    "torrent_url": "",
                    "tags": [],
                },
            }
        )
    )

    assert result.task.client_id == client.id
    assert called["client_id"] == client.id


def test_download_request_service_rejects_unknown_indexer_when_client_id_missing(download_tables):
    library = _create_library()
    _create_client(library)

    service = DownloadRequestService(qbittorrent_client_cls=object)

    with pytest.raises(ApiError) as exc_info:
        service.create_request(
            DownloadRequestCreateRequest.model_validate(
                {
                    "movie_number": "ABC-001",
                    "candidate": {
                        "source": "jackett",
                        "indexer_name": "missing",
                        "indexer_kind": "pt",
                        "title": "ABC-001",
                        "size_bytes": 123,
                        "seeders": 5,
                        "magnet_url": "magnet:?xt=urn:btih:ABCDEF123456",
                        "torrent_url": "",
                        "tags": [],
                    },
                }
            )
        )

    assert exc_info.value.code == "download_request_indexer_not_found"


def test_download_request_service_is_idempotent_per_client(download_tables):
    library = _create_library()
    client = _create_client(library)

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def add_candidate(self, **kwargs):
            return {
                "info_hash": "abcdef123456",
                "name": "ABC-001",
                "progress": 0.1,
                "state": "downloading",
                "save_path": "/downloads/a/ABC-001",
            }

    service = DownloadRequestService(qbittorrent_client_cls=FakeQBittorrentClient)
    payload = DownloadRequestCreateRequest.model_validate(
        {
            "client_id": client.id,
            "movie_number": "ABC-001",
            "candidate": {
                "source": "jackett",
                "indexer_name": "mteam",
                "indexer_kind": "pt",
                "title": "ABC-001",
                "size_bytes": 123,
                "seeders": 5,
                "magnet_url": "magnet:?xt=urn:btih:ABCDEF123456",
                "torrent_url": "",
                "tags": [],
            },
        }
    )

    first = service.create_request(payload)
    second = service.create_request(payload)

    assert first.created is True
    assert second.created is False
    assert DownloadTask.select().count() == 1


def test_download_sync_service_maps_remote_tasks_and_updates_existing(download_tables):
    library = _create_library()
    client = _create_client(library)
    task = DownloadTask.create(
        client=client,
        movie=None,
        name="old",
        info_hash="hash-1",
        save_path="/mnt/downloads/a/old",
        progress=0.0,
        download_state="queued",
        import_status="pending",
    )

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_torrents(self, *, client_id=None):
            assert client_id == client.id
            return [
                {
                    "info_hash": "hash-1",
                    "name": "ABC-001 4K",
                    "progress": 1.0,
                    "state": "uploading",
                    "save_path": "/downloads/a/ABC-001",
                },
                {
                    "info_hash": "hash-2",
                    "name": "SSIS-001",
                    "progress": 0.5,
                    "state": "downloading",
                    "save_path": "/downloads/a/SSIS-001",
                },
            ]

    summary = DownloadSyncService(qbittorrent_client_cls=FakeQBittorrentClient).sync_client(client.id)
    task = DownloadTask.get_by_id(task.id)

    assert summary.scanned_count == 2
    assert summary.created_count == 1
    assert summary.updated_count == 1
    assert task.movie == "ABC-001"
    assert task.download_state == "completed"
    assert task.save_path == "/mnt/downloads/a/ABC-001"


def test_download_sync_service_sync_all_clients_continues_when_one_client_fails(download_tables):
    library = _create_library()
    failing_client = _create_client(library, name="client-failing")
    healthy_client = _create_client(library, name="client-healthy", password="other-secret")

    class FakeQBittorrentClient:
        def __init__(self, client_id: int):
            self.client_id = client_id

        @classmethod
        def from_download_client(cls, download_client):
            return cls(download_client.id)

        def list_torrents(self, *, client_id=None):
            assert client_id == self.client_id
            if self.client_id == failing_client.id:
                raise Exception("boom")
            return [
                {
                    "info_hash": "hash-healthy",
                    "name": "ABC-002",
                    "progress": 1.0,
                    "state": "uploading",
                    "save_path": "/downloads/a/ABC-002",
                }
            ]

    summary = DownloadSyncService(qbittorrent_client_cls=FakeQBittorrentClient).sync_all_clients()

    created_task = DownloadTask.get(DownloadTask.client == healthy_client.id)
    assert summary["total_clients"] == 2
    assert summary["failed_count"] == 1
    assert summary["failed_client_ids"] == [failing_client.id]
    assert summary["created_count"] == 1
    assert created_task.movie == "ABC-002"


def test_download_client_delete_rejects_when_indexer_exists(download_tables):
    library = _create_library()
    client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=client,
    )

    with pytest.raises(ApiError) as exc_info:
        DownloadClientService.delete_client(client.id)

    assert exc_info.value.code == "download_client_in_use_by_indexers"


def test_download_sync_service_enqueues_auto_imports(download_tables, monkeypatch, tmp_path):
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
    downloads_dir = tmp_path / "downloads" / "ABC-001"
    downloads_dir.mkdir(parents=True)
    completed = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path=str(downloads_dir),
        progress=1.0,
        download_state="completed",
        import_status="pending",
    )
    DownloadTask.create(
        client=client,
        movie="ABC-002",
        name="ABC-002",
        info_hash="hash-2",
        save_path=str(tmp_path / "downloads" / "ABC-002"),
        progress=1.0,
        download_state="downloading",
        import_status="pending",
    )
    called = []

    monkeypatch.setattr(
        DownloadTaskService,
        "trigger_import",
        classmethod(lambda cls, task_id, allowed_statuses=None: called.append((task_id, allowed_statuses))),
    )

    summary = DownloadSyncService().enqueue_auto_imports()

    assert summary == {"queued_count": 1, "recovered_count": 0}
    assert called == [(completed.id, {"pending"})]


def test_download_sync_service_recovers_orphaned_running_import_before_requeue(
    download_tables, monkeypatch, tmp_path
):
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
    download_dir = tmp_path / "downloads" / "ABC-001"
    download_dir.mkdir(parents=True)
    task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path=str(download_dir),
        progress=1.0,
        download_state="completed",
        import_status="running",
    )
    orphaned_job = ImportJob.create(
        source_path=str(download_dir),
        library=library,
        download_task=task,
        state="running",
    )
    called = []

    monkeypatch.setattr(
        "src.service.transfers.download_sync_service.DownloadImportRunner.has_active_job",
        lambda import_job_id: False,
    )
    monkeypatch.setattr(
        DownloadTaskService,
        "trigger_import",
        classmethod(lambda cls, task_id, allowed_statuses=None: called.append((task_id, allowed_statuses))),
    )

    summary = DownloadSyncService().enqueue_auto_imports()

    task = DownloadTask.get_by_id(task.id)
    orphaned_job = ImportJob.get_by_id(orphaned_job.id)
    assert summary == {"queued_count": 1, "recovered_count": 1}
    assert task.import_status == "pending"
    assert orphaned_job.state == "failed"
    assert orphaned_job.finished_at is not None
    assert called == [(task.id, {"pending"})]


def test_download_task_service_trigger_import_marks_running_and_creates_job(download_tables, monkeypatch, tmp_path):
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
    download_dir = tmp_path / "downloads" / "ABC-001"
    download_dir.mkdir(parents=True)
    task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path=str(download_dir),
        progress=1.0,
        download_state="completed",
        import_status="pending",
    )
    submitted = {}

    monkeypatch.setattr(
        "src.service.transfers.download_task_service.DownloadImportRunner.submit",
        lambda import_job_id, fn, *args: submitted.update(
            {"import_job_id": import_job_id, "args": args}
        ),
    )

    response = DownloadTaskService.trigger_import(task.id)
    task = DownloadTask.get_by_id(task.id)

    assert response.status == "accepted"
    assert ImportJob.get_by_id(response.import_job_id).download_task_id == task.id
    assert task.import_status == "running"
    assert submitted["import_job_id"] == response.import_job_id
    assert submitted["args"] == (task.id, response.import_job_id)


def test_download_task_service_trigger_import_preserves_single_file_source_path(
    download_tables, monkeypatch, tmp_path
):
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
    download_file = tmp_path / "downloads" / "ABC-001.mp4"
    download_file.parent.mkdir(parents=True, exist_ok=True)
    download_file.write_bytes(b"x" * 10)
    task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path=str(download_file),
        progress=1.0,
        download_state="completed",
        import_status="pending",
    )

    monkeypatch.setattr(
        "src.service.transfers.download_task_service.DownloadImportRunner.submit",
        lambda import_job_id, fn, *args: None,
    )

    response = DownloadTaskService.trigger_import(task.id)

    assert ImportJob.get_by_id(response.import_job_id).source_path == str(download_file.resolve())


def test_download_task_service_trigger_import_rejects_non_completed_or_duplicate(download_tables, tmp_path):
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
    download_dir = tmp_path / "downloads" / "ABC-001"
    download_dir.mkdir(parents=True)
    pending_task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path=str(download_dir),
        progress=0.5,
        download_state="downloading",
        import_status="pending",
    )
    running_task = DownloadTask.create(
        client=client,
        movie="ABC-002",
        name="ABC-002",
        info_hash="hash-2",
        save_path=str(download_dir),
        progress=1.0,
        download_state="completed",
        import_status="running",
    )

    with pytest.raises(ApiError) as invalid_state:
        DownloadTaskService.trigger_import(pending_task.id)
    with pytest.raises(ApiError) as conflict:
        DownloadTaskService.trigger_import(running_task.id)

    assert invalid_state.value.code == "invalid_download_task_import"
    assert conflict.value.code == "download_task_import_conflict"


def test_download_task_service_run_import_job_marks_failure_when_bootstrap_fails(download_tables, tmp_path):
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
    missing_path = tmp_path / "downloads" / "missing"
    task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path=str(missing_path),
        progress=1.0,
        download_state="completed",
        import_status="running",
    )
    job = ImportJob.create(
        source_path=str(missing_path),
        library=library,
        download_task=task,
        state="pending",
    )

    DownloadTaskService._run_import_job(task.id, job.id)

    task = DownloadTask.get_by_id(task.id)
    job = ImportJob.get_by_id(job.id)
    assert task.import_status == "failed"
    assert job.state == "failed"
    assert job.finished_at is not None
