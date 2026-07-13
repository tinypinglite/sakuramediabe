from queue import Empty

import pytest

from src.api.exception.errors import ApiError
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
    SystemEvent,
    SystemNotification,
    VideoItem,
)
from src.schema.transfers.downloads import (
    DownloadCandidateResource,
    DownloadClientCreateRequest,
    DownloadClientProbeStorageTestRequest,
    DownloadClientProbeTestRequest,
    DownloadClientUpdateRequest,
    DownloadRequestCreateResponse,
    DownloadRequestCreateRequest,
    DownloadTaskResource,
)
from src.service.transfers.download_client_service import DownloadClientService
from src.service.transfers.download_request_service import DownloadRequestService
from src.service.transfers.download_search_service import DownloadSearchService
from src.service.transfers.subscribed_movie_auto_download_service import (
    SubscribedMovieAutoDownloadService,
)
from src.service.transfers.download_sync_service import DownloadSyncService
from src.service.transfers.download_task_service import DownloadTaskService
from src.service.transfers.common import build_movie_save_path, map_download_state, map_remote_path
from src.service.transfers.jackett_client import JackettClient
from src.service.transfers.qbittorrent_client import (
    QBittorrentClient,
    QBittorrentClientError,
    QBittorrentTorrentNotManagedError,
)
from src.service.transfers.download_progress_service import (
    DownloadProgressHub,
    DownloadProgressSubscription,
)


def test_ensure_add_success_accepts_legacy_ok_text():
    # qBittorrent 4.x 返回文本 "Ok." 仍应通过
    QBittorrentClient._ensure_add_success("Ok.")


def test_ensure_add_success_rejects_legacy_fails_text():
    with pytest.raises(QBittorrentClientError):
        QBittorrentClient._ensure_add_success("Fails.")


def test_ensure_add_success_accepts_qb5_metadata():
    # qBittorrent 5.x 返回 JSON 元数据，failure_count 为 0 视为成功
    QBittorrentClient._ensure_add_success(
        {"added_torrent_ids": ["abc"], "failure_count": 0, "pending_count": 0, "success_count": 1}
    )


def test_ensure_add_success_rejects_qb5_failure_metadata():
    with pytest.raises(QBittorrentClientError):
        QBittorrentClient._ensure_add_success(
            {"added_torrent_ids": [], "failure_count": 1, "pending_count": 0, "success_count": 0}
        )


def test_qbittorrent_client_reads_sync_delta_and_controls_managed_torrent():
    class FakeTorrent:
        hash = "hash-1"
        name = "ABC-001"
        progress = 0.2
        state = "downloading"
        save_path = "/downloads/a/ABC-001"
        tags = "sakuramedia,client:3"

    class FakeMainData:
        def delta(self):
            return {
                "full_update": True,
                "torrents": {
                    "hash-1": {
                        "name": "ABC-001",
                        "tags": "sakuramedia,client:3",
                        "progress": 0.2,
                        "state": "downloading",
                        "dlspeed": 123,
                    }
                },
                "torrents_removed": [],
                "server_state": {"dl_info_speed": 456, "up_info_speed": 7},
            }

    class FakeClient:
        def __init__(self):
            self.sync = type("Sync", (), {"maindata": FakeMainData()})()
            self.stopped = []
            self.started = []
            self.deleted = []

        def auth_log_in(self):
            return None

        def torrents_info(self, torrent_hashes=None):
            return [FakeTorrent()]

        def torrents_stop(self, torrent_hashes):
            self.stopped.append(torrent_hashes)

        def torrents_start(self, torrent_hashes):
            self.started.append(torrent_hashes)

        def torrents_delete(self, torrent_hashes, delete_files):
            self.deleted.append((torrent_hashes, delete_files))

    fake = FakeClient()
    qb = QBittorrentClient(base_url="http://qb", username="u", password="p", client=fake)

    delta = qb.get_torrent_progress_delta()
    assert delta["full_update"] is True
    assert delta["torrents"]["hash-1"]["dlspeed"] == 123
    qb.pause_torrent("hash-1", client_id=3)
    qb.resume_torrent("hash-1", client_id=3)
    assert qb.delete_torrent("hash-1", client_id=3, delete_files=True) is True
    assert fake.stopped == ["hash-1"]
    assert fake.started == ["hash-1"]
    assert fake.deleted == [("hash-1", True)]


def test_qbittorrent_client_rejects_unmanaged_torrent_control():
    class FakeTorrent:
        hash = "hash-1"
        name = "manual"
        progress = 0.2
        state = "downloading"
        save_path = "/downloads/a/manual"
        tags = "manual"

    class FakeClient:
        def auth_log_in(self):
            return None

        def torrents_info(self, torrent_hashes=None):
            return [FakeTorrent()]

        def torrents_stop(self, torrent_hashes):
            raise AssertionError("unmanaged torrent must not be stopped")

    qb = QBittorrentClient(base_url="http://qb", username="u", password="p", client=FakeClient())
    with pytest.raises(QBittorrentTorrentNotManagedError):
        qb.pause_torrent("hash-1", client_id=3)


@pytest.mark.parametrize(
    "raw_state, expected",
    [
        # qBittorrent 5.x 改名后的停止状态：无论下载中还是完成后被暂停，语义都是 paused
        ("stoppedDL", "paused"),
        ("stoppedUP", "paused"),
        # 旧名称继续兼容
        ("pausedDL", "paused"),
        ("pausedUP", "paused"),
        # 真正在上传/做种的状态归到 seeding，让前端能与 completed 区分展示
        ("uploading", "seeding"),
        ("stalledUP", "seeding"),
        ("queuedUP", "seeding"),
        ("forcedUP", "seeding"),
    ],
)
def test_map_download_state_handles_qb5_stopped_states(raw_state, expected):
    assert map_download_state(raw_state) == expected


@pytest.mark.parametrize("seeding_raw", ["uploading", "stalledUP", "queuedUP", "forcedUP"])
def test_is_download_complete_covers_seeding(seeding_raw):
    from src.service.transfers.common import is_download_complete

    normalized = map_download_state(seeding_raw)
    assert normalized == "seeding"
    # 做种态在业务上等价于"下载已完成、文件已写定"，可参与自动导入判定。
    assert is_download_complete(normalized) is True
    assert is_download_complete("completed") is True
    assert is_download_complete("downloading") is False
    # 暂停态即使发生在下载完成后，也不应被视作可自动导入的"完成"状态。
    assert is_download_complete("paused") is False


@pytest.mark.parametrize(
    "raw_state",
    [
        # 下载完成后的校验/移动窗口：progress 已是 1.0 但文件仍在写入/移动，绝不能判完成
        "checkingUP",
        "checkingDL",
        "checkingResumeData",
        "moving",
        # piece 已下完但仍处于下载态的尾段，同样不算完成
        "downloading",
        "forcedDL",
    ],
)
def test_map_download_state_does_not_complete_during_checking_or_moving(raw_state):
    # 回归：旧逻辑用 progress>=1 会把这些过渡态误判为 completed，触发对未写完文件的导入，
    # 导致内容指纹不稳定、去重失效而重复导入。
    assert map_download_state(raw_state) != "completed"


@pytest.mark.parametrize(
    "client_save_path,movie_number,expected",
    [
        ("/downloads/a", "ABC-001", "/downloads/a/ABC-001"),
        # client_save_path 带尾斜杠时不应产生双斜杠。
        ("/downloads/a/", "ABC-001", "/downloads/a/ABC-001"),
        # FC2 等带多段连字符的番号原样保留。
        ("/downloads/a", "FC2-PPV-123456", "/downloads/a/FC2-PPV-123456"),
        # 非法字符（含路径穿越）被替换成下划线，杜绝跳出下载目录。
        ("/downloads/a", "../ABC/001", "/downloads/a/ABC_001"),
        ("/downloads/a", "ABC:001*?", "/downloads/a/ABC_001"),
    ],
)
def test_build_movie_save_path_builds_safe_subdir(client_save_path, movie_number, expected):
    assert build_movie_save_path(client_save_path, movie_number) == expected


def test_build_movie_save_path_rejects_number_without_valid_chars():
    with pytest.raises(ApiError) as exc:
        build_movie_save_path("/downloads/a", "../")
    assert exc.value.code == "invalid_download_request_movie_number"


@pytest.fixture()
def download_tables(test_db):
    models = [
        Image,
        MovieSeries,
        Movie,
        VideoItem,
        MediaLibrary,
        Media,
        BackgroundTaskRun,
        SystemNotification,
        SystemEvent,
        DownloadClient,
        Indexer,
        DownloadTask,
        ImportJob,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db


def _create_library(name: str = "Main", root_path: str = "/library/main") -> MediaLibrary:
    return MediaLibrary.create(name=name, backend="local", backend_config={"root_path": root_path})


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


def _create_movie(
    movie_number: str,
    *,
    title: str | None = None,
    is_subscribed: bool = True,
) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{movie_number}",
        movie_number=movie_number,
        title=title or movie_number,
        is_subscribed=is_subscribed,
    )


def _candidate(
    movie_number: str,
    *,
    title: str,
    indexer_kind: str = "bt",
    seeders: int = 5,
    size_bytes: int = 5 * 1024 * 1024 * 1024,
    tags: list[str] | None = None,
    magnet_url: str = "magnet:?xt=urn:btih:ABCDEF123456",
    torrent_url: str = "",
    indexer_name: str = "mteam",
) -> DownloadCandidateResource:
    return DownloadCandidateResource(
        source="jackett",
        indexer_name=indexer_name,
        indexer_kind=indexer_kind,
        resolved_client_id=1,
        resolved_client_name="client-a",
        movie_number=movie_number,
        title=title,
        size_bytes=size_bytes,
        seeders=seeders,
        magnet_url=magnet_url,
        torrent_url=torrent_url,
        tags=tags or [],
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


def test_download_client_service_test_client_reports_qb_status(download_tables):
    library = _create_library()
    client = _create_client(library)

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            assert download_client.id == client.id
            return cls()

        def inspect_status(self):
            return {
                "version": "5.0.4",
                "web_api_version": "2.11.4",
            }

    result = DownloadClientService.test_client(
        client.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
    )

    assert result.healthy is True
    assert result.client_id == client.id
    assert result.client_name == "client-a"
    assert result.base_url == "http://localhost:8080"
    assert result.version == "5.0.4"
    assert result.web_api_version == "2.11.4"
    assert result.error is None


def test_download_client_service_test_client_returns_unhealthy_on_qb_error(download_tables):
    library = _create_library()
    client = _create_client(library)

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def inspect_status(self):
            raise QBittorrentClientError("login failed")

    result = DownloadClientService.test_client(
        client.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
    )

    assert result.healthy is False
    assert result.version is None
    assert result.web_api_version is None
    assert result.error is not None
    assert result.error.type == "qbittorrent_request_error"
    assert result.error.message == "login failed"


def test_download_client_service_test_client_requires_existing_client(download_tables):
    with pytest.raises(ApiError) as exc_info:
        DownloadClientService.test_client(404)

    assert exc_info.value.code == "download_client_not_found"


def test_download_client_service_storage_test_reports_mapping_and_hardlink_ok(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    client = _create_client(library, name="client-storage")
    client.local_root_path = str(tmp_path / "downloads")
    client.save()
    (tmp_path / "downloads").mkdir()
    (tmp_path / "library").mkdir()

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            assert download_client.id == client.id
            return cls()

        def list_directory_names(self, directory_path):
            assert directory_path == "/downloads/a/.sakuramedia-diagnostics/probe-ok"
            return ["sentinel.txt"]

    result = DownloadClientService.test_storage(
        client.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-ok",
    )

    assert result.healthy is True
    assert result.directory_mapping.status == "ok"
    assert result.directory_mapping.sentinel_visible_to_qb is True
    assert result.hardlink.status == "ok"
    assert result.hardlink.supported is True
    assert result.warnings == []
    assert not (tmp_path / "downloads" / ".sakuramedia-diagnostics" / "probe-ok").exists()
    assert not (tmp_path / "library" / ".sakuramedia-diagnostics" / "probe-ok").exists()


def test_download_client_service_storage_test_fails_when_sentinel_invisible(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    client = _create_client(library, name="client-storage")
    client.local_root_path = str(tmp_path / "downloads")
    client.save()
    (tmp_path / "downloads").mkdir()

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_directory_names(self, directory_path):
            return ["other.txt"]

    result = DownloadClientService.test_storage(
        client.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-missing",
    )

    assert result.healthy is False
    assert result.directory_mapping.status == "failed"
    assert result.directory_mapping.sentinel_visible_to_qb is False
    assert result.directory_mapping.error.type == "sentinel_not_visible_to_qb"
    assert result.hardlink.status == "skipped"
    assert not (tmp_path / "downloads" / ".sakuramedia-diagnostics" / "probe-missing").exists()


def test_download_client_service_storage_test_reports_qb_directory_error(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    client = _create_client(library, name="client-storage")
    client.local_root_path = str(tmp_path / "downloads")
    client.save()
    (tmp_path / "downloads").mkdir()

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_directory_names(self, directory_path):
            raise QBittorrentClientError("directory not found")

    result = DownloadClientService.test_storage(
        client.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-error",
    )

    assert result.healthy is False
    assert result.directory_mapping.error.type == "qbittorrent_request_error"
    assert result.directory_mapping.error.message == "directory not found"
    assert result.hardlink.status == "skipped"
    assert not (tmp_path / "downloads" / ".sakuramedia-diagnostics" / "probe-error").exists()


def test_download_client_service_storage_test_reports_missing_local_root(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    client = _create_client(library, name="client-storage")
    client.local_root_path = str(tmp_path / "missing-downloads")
    client.save()

    result = DownloadClientService.test_storage(
        client.id,
        qbittorrent_client_cls=object,
        probe_id="probe-missing-root",
    )

    assert result.healthy is False
    assert result.directory_mapping.error.type == "local_root_path_not_accessible"
    assert result.hardlink.status == "skipped"


def test_download_client_service_storage_test_warns_when_hardlink_fails(
    download_tables,
    tmp_path,
    monkeypatch,
):
    library = _create_library(root_path=str(tmp_path / "library"))
    client = _create_client(library, name="client-storage")
    client.local_root_path = str(tmp_path / "downloads")
    client.save()
    (tmp_path / "downloads").mkdir()
    (tmp_path / "library").mkdir()

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_directory_names(self, directory_path):
            return ["sentinel.txt"]

    def raise_cross_device(source, target):
        raise OSError("Invalid cross-device link")

    monkeypatch.setattr("src.service.transfers.download_client_service.os.link", raise_cross_device)

    result = DownloadClientService.test_storage(
        client.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-hardlink-fail",
    )

    assert result.healthy is True
    assert result.directory_mapping.status == "ok"
    assert result.hardlink.status == "failed"
    assert result.hardlink.supported is False
    assert result.hardlink.error.type == "hardlink_not_supported"
    assert result.warnings == ["下载目录到媒体库不支持硬链接，导入会回退为复制"]
    assert not (tmp_path / "downloads" / ".sakuramedia-diagnostics" / "probe-hardlink-fail").exists()
    assert not (tmp_path / "library" / ".sakuramedia-diagnostics" / "probe-hardlink-fail").exists()


def test_download_client_service_storage_test_keeps_existing_diagnostic_parent(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    client = _create_client(library, name="client-storage")
    client.local_root_path = str(tmp_path / "downloads")
    client.save()
    (tmp_path / "downloads" / ".sakuramedia-diagnostics").mkdir(parents=True)
    (tmp_path / "library" / ".sakuramedia-diagnostics").mkdir(parents=True)

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_directory_names(self, directory_path):
            return ["sentinel.txt"]

    result = DownloadClientService.test_storage(
        client.id,
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-existing-parent",
    )

    assert result.healthy is True
    assert (tmp_path / "downloads" / ".sakuramedia-diagnostics").is_dir()
    assert (tmp_path / "library" / ".sakuramedia-diagnostics").is_dir()
    assert not (tmp_path / "downloads" / ".sakuramedia-diagnostics" / "probe-existing-parent").exists()
    assert not (tmp_path / "library" / ".sakuramedia-diagnostics" / "probe-existing-parent").exists()


def test_download_client_service_storage_test_does_not_delete_existing_probe_dir(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    client = _create_client(library, name="client-storage")
    client.local_root_path = str(tmp_path / "downloads")
    client.save()
    existing_probe_dir = tmp_path / "downloads" / ".sakuramedia-diagnostics" / "probe-collision"
    existing_probe_dir.mkdir(parents=True)

    result = DownloadClientService.test_storage(
        client.id,
        qbittorrent_client_cls=object,
        probe_id="probe-collision",
    )

    assert result.healthy is False
    assert result.directory_mapping.error.type == "filesystem_error"
    assert existing_probe_dir.is_dir()


def test_download_client_service_probe_test_uses_payload_password(download_tables):
    _create_library()

    captured = {}

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            captured["base_url"] = download_client.base_url
            captured["username"] = download_client.username
            captured["password"] = download_client.password
            return cls()

        def inspect_status(self):
            return {"version": "5.0.4", "web_api_version": "2.11.4"}

    result = DownloadClientService.probe_test(
        DownloadClientProbeTestRequest(
            base_url="http://qb.example.com",
            username="alice",
            password="fresh-secret",
        ),
        qbittorrent_client_cls=FakeQBittorrentClient,
    )

    assert result.healthy is True
    assert result.client_id == 0
    assert result.client_name == ""
    assert result.base_url == "http://qb.example.com"
    assert captured["password"] == "fresh-secret"


def test_download_client_service_probe_test_merges_password_from_db(download_tables):
    library = _create_library()
    existing = _create_client(library, name="client-a", password="db-secret")

    captured = {}

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            captured["password"] = download_client.password
            return cls()

        def inspect_status(self):
            return {"version": "5.0.4", "web_api_version": "2.11.4"}

    result = DownloadClientService.probe_test(
        DownloadClientProbeTestRequest(
            base_url="http://qb.example.com",
            username="alice",
            password=None,
            client_id=existing.id,
        ),
        qbittorrent_client_cls=FakeQBittorrentClient,
    )

    assert result.healthy is True
    assert captured["password"] == "db-secret"


def test_download_client_service_probe_test_rejects_empty_password_without_client_id(download_tables):
    with pytest.raises(ApiError) as exc_info:
        DownloadClientService.probe_test(
            DownloadClientProbeTestRequest(
                base_url="http://qb.example.com",
                username="alice",
                password=None,
            ),
        )
    assert exc_info.value.code == "invalid_download_client_password"


def test_download_client_service_probe_storage_test_uses_payload_paths(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    (tmp_path / "downloads").mkdir()
    (tmp_path / "library").mkdir()

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            assert download_client.base_url == "http://qb.example.com"
            assert download_client.password == "fresh-secret"
            return cls()

        def list_directory_names(self, directory_path):
            assert directory_path == "/downloads/a/.sakuramedia-diagnostics/probe-new"
            return ["sentinel.txt"]

    result = DownloadClientService.probe_storage_test(
        DownloadClientProbeStorageTestRequest(
            base_url="http://qb.example.com",
            username="alice",
            password="fresh-secret",
            client_save_path="/downloads/a",
            local_root_path=str(tmp_path / "downloads"),
            media_library_id=library.id,
        ),
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-new",
    )

    assert result.healthy is True
    assert result.client_id == 0
    assert result.directory_mapping.status == "ok"
    assert result.hardlink.status == "ok"


def test_download_client_service_probe_storage_test_preserves_existing_library_probe_dir(
    download_tables,
    tmp_path,
):
    library = _create_library(root_path=str(tmp_path / "library"))
    downloads_root = tmp_path / "downloads"
    library_root = tmp_path / "library"
    library_probe_dir = library_root / ".sakuramedia-diagnostics" / "probe-existing"
    downloads_root.mkdir()
    library_probe_dir.mkdir(parents=True)
    keep_file = library_probe_dir / "keep.txt"
    keep_file.write_text("user-owned\n", encoding="utf-8")

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_directory_names(self, directory_path):
            return ["sentinel.txt"]

    result = DownloadClientService.probe_storage_test(
        DownloadClientProbeStorageTestRequest(
            base_url="http://qb.example.com",
            username="alice",
            password="fresh-secret",
            client_save_path="/downloads/a",
            local_root_path=str(downloads_root),
            media_library_id=library.id,
        ),
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-existing",
    )

    assert result.hardlink.status == "ok"
    assert library_probe_dir.exists()
    assert keep_file.read_text(encoding="utf-8") == "user-owned\n"
    assert not (library_probe_dir / "sentinel.link").exists()


def test_download_client_service_probe_storage_test_merges_password_from_db(download_tables, tmp_path):
    library = _create_library(root_path=str(tmp_path / "library"))
    existing = _create_client(library, name="client-a", password="db-secret")
    (tmp_path / "downloads").mkdir()
    (tmp_path / "library").mkdir()

    captured = {}

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            captured["password"] = download_client.password
            return cls()

        def list_directory_names(self, directory_path):
            return ["sentinel.txt"]

    DownloadClientService.probe_storage_test(
        DownloadClientProbeStorageTestRequest(
            base_url="http://qb.example.com",
            username="alice",
            password=None,
            client_save_path="/downloads/a",
            local_root_path=str(tmp_path / "downloads"),
            media_library_id=library.id,
            client_id=existing.id,
        ),
        qbittorrent_client_cls=FakeQBittorrentClient,
        probe_id="probe-merge",
    )

    assert captured["password"] == "db-secret"


def test_download_client_service_probe_storage_test_rejects_missing_media_library(download_tables, tmp_path):
    with pytest.raises(ApiError) as exc_info:
        DownloadClientService.probe_storage_test(
            DownloadClientProbeStorageTestRequest(
                base_url="http://qb.example.com",
                username="alice",
                password="secret",
                client_save_path="/downloads/a",
                local_root_path=str(tmp_path / "downloads"),
                media_library_id=404,
            ),
        )
    assert exc_info.value.code == "media_library_not_found"


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


def test_jackett_client_search_uses_numeric_query_for_fc2_number(download_tables):
    class FakeResponse:
        text = "<rss><channel></channel></rss>"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self):
            self.calls = []

        def get(self, url, params):
            self.calls.append({"url": url, "params": params})
            return FakeResponse()

    library = _create_library()
    download_client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=download_client,
    )
    fake_http_client = FakeHttpClient()
    JackettClient(api_key="secret", client=fake_http_client).search("FC2-4861514")

    assert fake_http_client.calls[0]["params"]["q"] == "4861514"


def test_jackett_client_search_uses_numeric_query_for_fc2_ppv_number(download_tables):
    class FakeResponse:
        text = "<rss><channel></channel></rss>"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self):
            self.calls = []

        def get(self, url, params):
            self.calls.append({"url": url, "params": params})
            return FakeResponse()

    library = _create_library()
    download_client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=download_client,
    )
    fake_http_client = FakeHttpClient()
    JackettClient(api_key="secret", client=fake_http_client).search("FC2-PPV-4861514")

    assert fake_http_client.calls[0]["params"]["q"] == "4861514"


def test_jackett_client_search_keeps_non_fc2_query(download_tables):
    class FakeResponse:
        text = "<rss><channel></channel></rss>"

        def raise_for_status(self):
            return None

    class FakeHttpClient:
        def __init__(self):
            self.calls = []

        def get(self, url, params):
            self.calls.append({"url": url, "params": params})
            return FakeResponse()

    library = _create_library()
    download_client = _create_client(library)
    Indexer.create(
        name="mteam",
        url="http://jackett/api",
        kind="pt",
        download_client=download_client,
    )
    fake_http_client = FakeHttpClient()
    JackettClient(api_key="secret", client=fake_http_client).search("ABC-001")

    assert fake_http_client.calls[0]["params"]["q"] == "ABC-001"


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


def test_jackett_client_routes_magnet_in_link_to_magnet_url(download_tables):
    # 只有磁力链没有 .torrent 文件时，Jackett 会把磁力链塞进 <link>/<guid>，
    # 解析时应分流到 magnet_url，而不是误进 torrent_url
    class FakeResponse:
        text = """
        <rss>
          <channel>
            <item>
              <title>ABC-001</title>
              <size>3221225472</size>
              <link>magnet:?xt=urn:btih:HASHLINK&amp;dn=ABC-001</link>
              <guid>magnet:?xt=urn:btih:HASHLINK&amp;dn=ABC-001</guid>
              <torznab:attr name="seeders" value="5"/>
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
    assert results[0].magnet_url == "magnet:?xt=urn:btih:HASHLINK&dn=ABC-001"
    assert results[0].torrent_url == ""


def test_jackett_client_prefers_magneturl_attr_and_keeps_torrent_file_link(download_tables):
    # magneturl 属性给磁力链、<link> 给 .torrent 文件地址时，两者应各归其位
    class FakeResponse:
        text = """
        <rss>
          <channel>
            <item>
              <title>ABC-001</title>
              <size>3221225472</size>
              <link>https://indexer.example/download/1.torrent</link>
              <guid>https://indexer.example/details/1</guid>
              <torznab:attr name="seeders" value="5"/>
              <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:HASHATTR"/>
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
    assert results[0].magnet_url == "magnet:?xt=urn:btih:HASHATTR"
    assert results[0].torrent_url == "https://indexer.example/download/1.torrent"


def test_download_search_service_rejects_invalid_indexer_kind(download_tables):
    with pytest.raises(ApiError) as exc_info:
        DownloadSearchService().search_candidates(movie_number="ABC-001", indexer_kind="ftp")
    assert exc_info.value.code == "invalid_download_candidate_indexer_kind"


def test_download_request_service_adds_task_and_passes_movie_subdir(download_tables):
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
    # 提交时按番号指定独立子目录，落盘路径不再是下载根目录。
    assert called["save_path"] == "/downloads/a/ABC-001"
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
    # qB 原始 state=uploading 归一到 seeding（做种态即"下载完成 + 正在做种"），前端可用不同 badge 展示。
    assert task.download_state == "seeding"
    assert task.save_path == "/mnt/downloads/a/ABC-001"


def test_download_sync_service_prunes_ghost_tasks_missing_from_remote(download_tables):
    # qB 侧手动删掉的种子（本系统 API 没走过），本地会残留 DownloadTask 幽灵记录，前端列表接口
    # 一直返回；sync 后应做反向对账把不在 remote 集合里的本地任务清掉。
    library = _create_library()
    client = _create_client(library)
    kept = DownloadTask.create(
        client=client, movie="ABC-001", name="ABC-001", info_hash="hash-live",
        save_path="/mnt/downloads/a/ABC-001", progress=1.0,
        download_state="downloading", import_status="pending",
    )
    ghost_pending = DownloadTask.create(
        client=client, movie="GHOST-1", name="ghost pending", info_hash="hash-ghost-pending",
        save_path="/mnt/downloads/a/ghost1", progress=0.4,
        download_state="downloading", import_status="pending",
    )
    # 方案 A：completed 也一起清（历史查 ImportJob 表，不靠 DownloadTask 台账）
    ghost_completed = DownloadTask.create(
        client=client, movie="GHOST-2", name="ghost completed", info_hash="hash-ghost-completed",
        save_path="/mnt/downloads/a/ghost2", progress=1.0,
        download_state="completed", import_status="completed",
    )

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_torrents(self, *, client_id=None):
            return [{
                "info_hash": "hash-live", "name": "ABC-001", "progress": 1.0,
                "state": "downloading", "save_path": "/downloads/a/ABC-001",
            }]

    summary = DownloadSyncService(qbittorrent_client_cls=FakeQBittorrentClient).sync_client(client.id)

    assert summary.removed_count == 2
    assert DownloadTask.get_or_none(DownloadTask.id == kept.id) is not None
    assert DownloadTask.get_or_none(DownloadTask.id == ghost_pending.id) is None
    assert DownloadTask.get_or_none(DownloadTask.id == ghost_completed.id) is None


def test_download_sync_service_keeps_ghost_when_import_running_or_pending_job(download_tables):
    # 白名单：import_status=running 或有 pending/running ImportJob 的任务，即使 qB 侧已消失
    # 也不能清，删了会破坏 in-flight 的导入状态；等 runner 走完再由后续 sync 收尾。
    library = _create_library()
    client = _create_client(library)
    running_task = DownloadTask.create(
        client=client, movie="RUN-1", name="running", info_hash="hash-running",
        save_path="/mnt/downloads/a/run1", progress=1.0,
        download_state="completed", import_status="running",
    )
    pending_job_task = DownloadTask.create(
        client=client, movie="RUN-2", name="pending job", info_hash="hash-pending-job",
        save_path="/mnt/downloads/a/run2", progress=1.0,
        download_state="completed", import_status="pending",
    )
    running_job_task = DownloadTask.create(
        client=client, movie="RUN-3", name="running job", info_hash="hash-running-job",
        save_path="/mnt/downloads/a/run3", progress=1.0,
        download_state="completed", import_status="pending",
    )
    # 已完成的 ImportJob 不算白名单 —— 关联的 DownloadTask 依然会被清（方案 A）
    finished_job_task = DownloadTask.create(
        client=client, movie="OLD-1", name="finished job", info_hash="hash-finished-job",
        save_path="/mnt/downloads/a/old1", progress=1.0,
        download_state="completed", import_status="completed",
    )
    ImportJob.create(source_path="/x/run2", library=library, download_task=pending_job_task, state="pending")
    ImportJob.create(source_path="/x/run3", library=library, download_task=running_job_task, state="running")
    finished_job = ImportJob.create(
        source_path="/x/old1", library=library, download_task=finished_job_task, state="completed",
    )

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_torrents(self, *, client_id=None):
            # 远端只保留一个非白名单种子，避免 remote_hashes 为空触发保底跳过。
            return [{
                "info_hash": "hash-live", "name": "keep-alive", "progress": 0.1,
                "state": "downloading", "save_path": "/downloads/a/live",
            }]

    summary = DownloadSyncService(qbittorrent_client_cls=FakeQBittorrentClient).sync_client(client.id)

    # 只有 finished_job_task 被清；其余 3 个都进白名单
    assert summary.removed_count == 1
    assert DownloadTask.get_or_none(DownloadTask.id == running_task.id) is not None
    assert DownloadTask.get_or_none(DownloadTask.id == pending_job_task.id) is not None
    assert DownloadTask.get_or_none(DownloadTask.id == running_job_task.id) is not None
    assert DownloadTask.get_or_none(DownloadTask.id == finished_job_task.id) is None
    # 关联 ImportJob 的 FK 是 SET NULL：DownloadTask 删掉后 ImportJob 台账仍在，只是断链
    finished_job = ImportJob.get_by_id(finished_job.id)
    assert finished_job.download_task_id is None
    assert finished_job.state == "completed"


def test_download_sync_service_skips_prune_when_qb_returns_empty(download_tables):
    # 保底：qB 返 0 条时不 prune，避免 tag 被误清 / 异常空返回一次抹掉所有本地记录。
    library = _create_library()
    client = _create_client(library)
    survivor = DownloadTask.create(
        client=client, movie="ABC-001", name="ABC-001", info_hash="hash-1",
        save_path="/mnt/downloads/a/ABC-001", progress=0.5,
        download_state="downloading", import_status="pending",
    )

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def list_torrents(self, *, client_id=None):
            return []

    summary = DownloadSyncService(qbittorrent_client_cls=FakeQBittorrentClient).sync_client(client.id)

    assert summary.scanned_count == 0
    assert summary.removed_count == 0
    assert DownloadTask.get_or_none(DownloadTask.id == survivor.id) is not None


def test_download_sync_service_prune_isolates_by_client(download_tables):
    # sync 只 prune 当前 client 的幽灵；另一个 client 的记录即使 hash 不在本次 remote 列表里
    # 也不能被误删——DELETE WHERE 条件里必须带 DownloadTask.client == client_id。
    library = _create_library()
    client_a = _create_client(library, name="client-a")
    client_b = _create_client(library, name="client-b")
    ghost_a = DownloadTask.create(
        client=client_a, movie="A-001", name="ghost-a", info_hash="hash-ghost-a",
        save_path="/mnt/downloads/a/ghost", progress=0.3,
        download_state="downloading", import_status="pending",
    )
    # client_b 的记录 hash 恰好不在 A 的 remote 列表里；不能被 A 的 sync 误伤
    kept_b = DownloadTask.create(
        client=client_b, movie="B-001", name="untouched-b", info_hash="hash-only-b",
        save_path="/mnt/downloads/b/kept", progress=0.7,
        download_state="downloading", import_status="pending",
    )

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls(download_client.id)

        def __init__(self, client_id=None):
            self.client_id = client_id

        def list_torrents(self, *, client_id=None):
            # A 只报一个 live 种子（remote_hashes 非空，触发 prune）
            return [{
                "info_hash": "hash-a-live", "name": "A-alive", "progress": 0.9,
                "state": "downloading", "save_path": "/downloads/a/live",
            }]

    summary = DownloadSyncService(qbittorrent_client_cls=FakeQBittorrentClient).sync_client(client_a.id)

    assert summary.removed_count == 1
    assert DownloadTask.get_or_none(DownloadTask.id == ghost_a.id) is None
    # 关键断言：B 的记录必须完好保留
    assert DownloadTask.get_or_none(DownloadTask.id == kept_b.id) is not None


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
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
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
        classmethod(
            lambda cls, task_id, allowed_statuses=None, trigger_type="manual": called.append(
                (task_id, allowed_statuses, trigger_type)
            )
        ),
    )

    summary = DownloadSyncService().enqueue_auto_imports()

    assert summary == {"queued_count": 1, "recovered_count": 0}
    assert called == [(completed.id, {"pending"}, "internal")]


def test_download_sync_service_enqueues_seeding_tasks_alongside_completed(
    download_tables, monkeypatch, tmp_path
):
    # 做种态在业务上等价于"下载完成，正在做种"，同样应被 auto_import 拾起，避免用户开启做种后
    # 任务卡在 seeding 状态里永远进不去导入流程。
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    client = DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path=str(tmp_path / "downloads"),
        media_library=library,
    )
    (tmp_path / "downloads" / "ABC-100").mkdir(parents=True)
    (tmp_path / "downloads" / "ABC-101").mkdir(parents=True)
    seeding_task = DownloadTask.create(
        client=client,
        movie="ABC-100",
        name="ABC-100",
        info_hash="hash-seed",
        save_path=str(tmp_path / "downloads" / "ABC-100"),
        progress=1.0,
        download_state="seeding",
        import_status="pending",
    )
    completed_task = DownloadTask.create(
        client=client,
        movie="ABC-101",
        name="ABC-101",
        info_hash="hash-done",
        save_path=str(tmp_path / "downloads" / "ABC-101"),
        progress=1.0,
        download_state="completed",
        import_status="pending",
    )
    called = []
    monkeypatch.setattr(
        DownloadTaskService,
        "trigger_import",
        classmethod(
            lambda cls, task_id, allowed_statuses=None, trigger_type="manual": called.append(task_id)
        ),
    )

    summary = DownloadSyncService().enqueue_auto_imports()

    assert summary == {"queued_count": 2, "recovered_count": 0}
    assert sorted(called) == sorted([seeding_task.id, completed_task.id])


def test_download_sync_service_recovers_orphaned_running_import_before_requeue(
    download_tables, monkeypatch, tmp_path
):
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
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
        task_run=BackgroundTaskRun.create(
            task_key="download_task_import",
            task_name="下载任务导入 ABC-001",
            trigger_type="internal",
            state="running",
            owner_pid=999999,
        ),
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
        classmethod(
            lambda cls, task_id, allowed_statuses=None, trigger_type="manual": called.append(
                (task_id, allowed_statuses, trigger_type)
            )
        ),
    )

    summary = DownloadSyncService().enqueue_auto_imports()

    task = DownloadTask.get_by_id(task.id)
    orphaned_job = ImportJob.get_by_id(orphaned_job.id)
    task_run = BackgroundTaskRun.get_by_id(orphaned_job.task_run_id)
    assert summary == {"queued_count": 1, "recovered_count": 1}
    assert task.import_status == "pending"
    assert orphaned_job.state == "failed"
    assert orphaned_job.finished_at is not None
    assert task_run.state == "failed"
    assert task_run.error_message == "下载导入线程已中断，任务已失败"
    assert called == [(task.id, {"pending"}, "internal")]


def test_download_sync_service_recover_orphaned_imports_only_does_not_enqueue_new_imports(
    download_tables, monkeypatch, tmp_path
):
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    client = DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path=str(tmp_path / "downloads"),
        media_library=library,
    )
    download_dir = tmp_path / "downloads" / "ABC-003"
    download_dir.mkdir(parents=True)
    task = DownloadTask.create(
        client=client,
        movie="ABC-003",
        name="ABC-003",
        info_hash="hash-3",
        save_path=str(download_dir),
        progress=1.0,
        download_state="completed",
        import_status="running",
    )
    orphaned_job = ImportJob.create(
        source_path=str(download_dir),
        library=library,
        download_task=task,
        task_run=BackgroundTaskRun.create(
            task_key="download_task_import",
            task_name="下载任务导入 ABC-003",
            trigger_type="internal",
            state="running",
            owner_pid=999999,
        ),
        state="running",
    )

    monkeypatch.setattr(
        "src.service.transfers.download_sync_service.DownloadImportRunner.has_active_job",
        lambda import_job_id: False,
    )
    monkeypatch.setattr(
        DownloadTaskService,
        "trigger_import",
        classmethod(
            lambda cls, task_id, allowed_statuses=None, trigger_type="manual": (_ for _ in ()).throw(
                AssertionError("recover_only should not enqueue new imports")
            )
        ),
    )

    summary = DownloadSyncService().recover_orphaned_imports_only()

    task = DownloadTask.get_by_id(task.id)
    orphaned_job = ImportJob.get_by_id(orphaned_job.id)
    task_run = BackgroundTaskRun.get_by_id(orphaned_job.task_run_id)
    assert summary == {"recovered_count": 1}
    assert task.import_status == "pending"
    assert orphaned_job.state == "failed"
    assert orphaned_job.finished_at is not None
    assert task_run.state == "failed"
    assert task_run.error_message == "下载导入线程已中断，任务已失败"


def test_download_sync_service_does_not_backfill_missing_task_run_id_for_legacy_jobs(
    download_tables, monkeypatch, tmp_path
):
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    client = DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path=str(tmp_path / "downloads"),
        media_library=library,
    )
    download_dir = tmp_path / "downloads" / "ABC-004"
    download_dir.mkdir(parents=True)
    task = DownloadTask.create(
        client=client,
        movie="ABC-004",
        name="ABC-004",
        info_hash="hash-4",
        save_path=str(download_dir),
        progress=1.0,
        download_state="completed",
        import_status="running",
    )
    orphaned_job = ImportJob.create(
        source_path=str(download_dir),
        library=library,
        download_task=task,
        task_run=None,
        state="running",
    )

    monkeypatch.setattr(
        "src.service.transfers.download_sync_service.DownloadImportRunner.has_active_job",
        lambda import_job_id: False,
    )
    monkeypatch.setattr(
        "src.service.transfers.download_sync_service.ActivityService.recover_task_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not recover missing task_run_id")),
    )

    summary = DownloadSyncService().recover_orphaned_imports_only()

    task = DownloadTask.get_by_id(task.id)
    orphaned_job = ImportJob.get_by_id(orphaned_job.id)
    assert summary == {"recovered_count": 1}
    assert task.import_status == "pending"
    assert orphaned_job.state == "failed"
    assert orphaned_job.finished_at is not None
    assert orphaned_job.task_run_id is None


def test_download_sync_service_keeps_activity_running_when_owner_process_is_still_alive(download_tables, monkeypatch, tmp_path):
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
    client = DownloadClient.create(
        name="client-a",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/a",
        local_root_path=str(tmp_path / "downloads"),
        media_library=library,
    )
    download_dir = tmp_path / "downloads" / "ABC-002"
    download_dir.mkdir(parents=True)
    task_run = BackgroundTaskRun.create(
        task_key="download_task_import",
        task_name="下载任务导入 ABC-002",
        trigger_type="manual",
        state="running",
    )
    task = DownloadTask.create(
        client=client,
        movie="ABC-002",
        name="ABC-002",
        info_hash="hash-2",
        save_path=str(download_dir),
        progress=1.0,
        download_state="completed",
        import_status="running",
    )
    ImportJob.create(
        source_path=str(download_dir),
        library=library,
        download_task=task,
        task_run=task_run,
        state="running",
    )

    monkeypatch.setattr(
        "src.service.transfers.download_sync_service.DownloadImportRunner.has_active_job",
        lambda import_job_id: False,
    )

    summary = DownloadSyncService().enqueue_auto_imports()

    task = DownloadTask.get_by_id(task.id)
    task_run = BackgroundTaskRun.get_by_id(task_run.id)
    assert summary == {"queued_count": 0, "recovered_count": 0}
    assert task.import_status == "running"
    assert task_run.state == "running"


def test_download_task_service_trigger_import_marks_running_and_creates_job(download_tables, monkeypatch, tmp_path):
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
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
    assert len(submitted["args"]) == 3
    assert submitted["args"][:2] == (task.id, response.import_job_id)
    assert response.task_run_id > 0
    assert BackgroundTaskRun.get_by_id(response.task_run_id).task_key == "download_task_import"
    assert ImportJob.get_by_id(response.import_job_id).task_run_id == response.task_run_id


def test_download_task_service_trigger_import_preserves_single_file_source_path(
    download_tables, monkeypatch, tmp_path
):
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
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
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
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
    with pytest.raises(ApiError):
        DownloadTaskService.trigger_import(running_task.id)

    assert invalid_state.value.code == "invalid_download_task_import"


def test_subscribed_movie_auto_download_selects_only_subscribed_movies_without_media_and_without_download_tasks(
    download_tables,
):
    library = _create_library()
    client = _create_client(library)
    candidate_movie = _create_movie("ABC-001", title="candidate")
    playable_movie = _create_movie("ABC-002", title="playable")
    movie_with_task = _create_movie("ABC-003", title="with-task")
    _create_movie("ABC-004", title="unsubscribed", is_subscribed=False)
    Media.create(
        movie=playable_movie,
        library=library,
        path="/library/main/ABC-002.mp4",
        valid=True,
    )
    DownloadTask.create(
        client=client,
        movie=movie_with_task.movie_number,
        name=movie_with_task.title,
        info_hash="hash-existing",
        save_path="/mnt/downloads/a/ABC-003",
        progress=0.0,
        download_state="failed",
        import_status="failed",
    )

    service = SubscribedMovieAutoDownloadService()

    candidate_movies = service._list_candidate_movies()

    assert [movie.movie_number for movie in candidate_movies] == [candidate_movie.movie_number]


def test_subscribed_movie_auto_download_skips_movies_with_only_invalid_media(download_tables):
    library = _create_library()
    invalid_media_movie = _create_movie("ABC-010", title="only-invalid")
    Media.create(
        movie=invalid_media_movie,
        library=library,
        path="/library/main/ABC-010.mp4",
        valid=False,
    )

    candidate_movies = SubscribedMovieAutoDownloadService()._list_candidate_movies()

    assert invalid_media_movie.movie_number not in [movie.movie_number for movie in candidate_movies]


def test_subscribed_movie_auto_download_prefers_4k_candidate_even_when_non_4k_has_more_seeders(
    download_tables,
):
    _create_library()
    movie = _create_movie("ABC-001")
    submitted = {}

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            assert movie_number == movie.movie_number
            assert indexer_kind is None
            return [
                _candidate(
                    movie.movie_number,
                    title="ABC-001 regular",
                    seeders=20,
                    tags=[],
                ),
                _candidate(
                    movie.movie_number,
                    title="ABC-001 4K",
                    seeders=3,
                    tags=["4K"],
                ),
            ]

    class FakeRequestService:
        def create_request(self, payload):
            submitted["payload"] = payload
            return DownloadRequestCreateResponse(
                task=DownloadTaskResource(
                    id=1,
                    client_id=1,
                    movie_number=payload.movie_number,
                    name=payload.candidate.title,
                    info_hash="hash-1",
                    save_path="/downloads/ABC-001",
                    progress=0.0,
                    download_state="queued",
                    import_status="pending",
                    created_at=movie.created_at,
                    updated_at=movie.updated_at,
                ),
                created=True,
            )

    summary = SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert submitted["payload"].candidate.title == "ABC-001 4K"
    assert summary["submitted_movies"] == 1


def test_subscribed_movie_auto_download_prefers_pt_within_same_4k_bucket(download_tables):
    _create_library()
    movie = _create_movie("ABC-001")
    submitted = {}

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            return [
                _candidate(movie_number, title="ABC-001 4K BT", indexer_kind="bt", tags=["4K"]),
                _candidate(movie_number, title="ABC-001 4K PT", indexer_kind="pt", tags=["4K"]),
            ]

    class FakeRequestService:
        def create_request(self, payload):
            submitted["payload"] = payload
            return DownloadRequestCreateResponse(
                task=DownloadTaskResource(
                    id=1,
                    client_id=1,
                    movie_number=movie.movie_number,
                    name=payload.candidate.title,
                    info_hash="hash-1",
                    save_path="/downloads/ABC-001",
                    progress=0.0,
                    download_state="queued",
                    import_status="pending",
                    created_at=movie.created_at,
                    updated_at=movie.updated_at,
                ),
                created=True,
            )

    SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert submitted["payload"].candidate.title == "ABC-001 4K PT"


def test_subscribed_movie_auto_download_prefers_subtitle_within_same_priority_bucket(download_tables):
    _create_library()
    movie = _create_movie("ABC-001")
    submitted = {}

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            return [
                _candidate(movie_number, title="ABC-001 PT regular", indexer_kind="pt", tags=[]),
                _candidate(movie_number, title="ABC-001 PT subtitle", indexer_kind="pt", tags=["中字"]),
            ]

    class FakeRequestService:
        def create_request(self, payload):
            submitted["payload"] = payload
            return DownloadRequestCreateResponse(
                task=DownloadTaskResource(
                    id=1,
                    client_id=1,
                    movie_number=movie.movie_number,
                    name=payload.candidate.title,
                    info_hash="hash-1",
                    save_path="/downloads/ABC-001",
                    progress=0.0,
                    download_state="queued",
                    import_status="pending",
                    created_at=movie.created_at,
                    updated_at=movie.updated_at,
                ),
                created=True,
            )

    SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert submitted["payload"].candidate.title == "ABC-001 PT subtitle"


def test_subscribed_movie_auto_download_filters_low_seeders_and_out_of_range_size(download_tables):
    _create_library()
    movie = _create_movie("ABC-001")
    submitted = {}

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            return [
                _candidate(movie_number, title="too small", seeders=10, size_bytes=500 * 1024 * 1024),
                _candidate(movie_number, title="too large", seeders=10, size_bytes=50 * 1024 * 1024 * 1024),
                _candidate(movie_number, title="too few seeders", seeders=2),
                _candidate(movie_number, title="valid", seeders=3, size_bytes=4 * 1024 * 1024 * 1024),
            ]

    class FakeRequestService:
        def create_request(self, payload):
            submitted["payload"] = payload
            return DownloadRequestCreateResponse(
                task=DownloadTaskResource(
                    id=1,
                    client_id=1,
                    movie_number=movie.movie_number,
                    name=payload.candidate.title,
                    info_hash="hash-1",
                    save_path="/downloads/ABC-001",
                    progress=0.0,
                    download_state="queued",
                    import_status="pending",
                    created_at=movie.created_at,
                    updated_at=movie.updated_at,
                ),
                created=True,
            )

    summary = SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert submitted["payload"].candidate.title == "valid"
    assert summary["no_candidate_movies"] == 0


def test_subscribed_movie_auto_download_counts_no_candidate_when_search_returns_empty_or_all_filtered(
    download_tables,
):
    _create_library()
    empty_movie = _create_movie("ABC-001")
    filtered_movie = _create_movie("ABC-002")

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            if movie_number == empty_movie.movie_number:
                return []
            return [_candidate(movie_number, title="bad", seeders=1)]

    class FakeRequestService:
        def create_request(self, payload):
            raise AssertionError("submit should not be called when no candidate is available")

    summary = SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert summary["submitted_movies"] == 0
    assert summary["no_candidate_movies"] == 2
    assert summary["no_candidate_movie_numbers"] == [empty_movie.movie_number, filtered_movie.movie_number]


def test_subscribed_movie_auto_download_continues_after_search_error(download_tables):
    _create_library()
    failing_movie = _create_movie("ABC-001")
    success_movie = _create_movie("ABC-002")
    submitted = []

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            if movie_number == failing_movie.movie_number:
                raise ApiError(502, "download_candidate_search_failed", "search failed")
            return [_candidate(movie_number, title=f"{movie_number} valid", tags=["4K"])]

    class FakeRequestService:
        def create_request(self, payload):
            submitted.append(payload.movie_number)
            return DownloadRequestCreateResponse(
                task=DownloadTaskResource(
                    id=1,
                    client_id=1,
                    movie_number=payload.movie_number,
                    name=payload.candidate.title,
                    info_hash="hash-1",
                    save_path="/downloads/ABC-001",
                    progress=0.0,
                    download_state="queued",
                    import_status="pending",
                    created_at=success_movie.created_at,
                    updated_at=success_movie.updated_at,
                ),
                created=True,
            )

    summary = SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert submitted == [success_movie.movie_number]
    assert summary["failed_movies"] == 1
    assert summary["submitted_movies"] == 1
    assert summary["failed_items"][0]["movie_number"] == failing_movie.movie_number
    assert summary["failed_items"][0]["stage"] == "search"


def test_subscribed_movie_auto_download_continues_after_submit_error(download_tables):
    _create_library()
    failing_movie = _create_movie("ABC-001")
    success_movie = _create_movie("ABC-002")
    submitted = []

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            return [_candidate(movie_number, title=f"{movie_number} valid", tags=["4K"])]

    class FakeRequestService:
        def create_request(self, payload):
            if payload.movie_number == failing_movie.movie_number:
                raise ApiError(502, "download_request_failed", "submit failed")
            submitted.append(payload.movie_number)
            return DownloadRequestCreateResponse(
                task=DownloadTaskResource(
                    id=1,
                    client_id=1,
                    movie_number=payload.movie_number,
                    name=payload.candidate.title,
                    info_hash="hash-1",
                    save_path="/downloads/ABC-001",
                    progress=0.0,
                    download_state="queued",
                    import_status="pending",
                    created_at=success_movie.created_at,
                    updated_at=success_movie.updated_at,
                ),
                created=True,
            )

    summary = SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert submitted == [success_movie.movie_number]
    assert summary["failed_movies"] == 1
    assert summary["submitted_movies"] == 1
    assert summary["failed_items"][0]["movie_number"] == failing_movie.movie_number
    assert summary["failed_items"][0]["stage"] == "submit"


def test_subscribed_movie_auto_download_treats_non_created_request_as_skipped(download_tables):
    _create_library()
    movie = _create_movie("ABC-001")

    class FakeSearchService:
        def search_candidates(self, *, movie_number, indexer_kind=None):
            return [_candidate(movie_number, title="ABC-001 4K", tags=["4K"])]

    class FakeRequestService:
        def create_request(self, payload):
            return DownloadRequestCreateResponse(
                task=DownloadTaskResource(
                    id=1,
                    client_id=1,
                    movie_number=movie.movie_number,
                    name=payload.candidate.title,
                    info_hash="hash-1",
                    save_path="/downloads/ABC-001",
                    progress=0.0,
                    download_state="queued",
                    import_status="pending",
                    created_at=movie.created_at,
                    updated_at=movie.updated_at,
                ),
                created=False,
            )

    summary = SubscribedMovieAutoDownloadService(
        download_search_service=FakeSearchService(),
        download_request_service=FakeRequestService(),
    ).run()

    assert summary["submitted_movies"] == 0
    assert summary["skipped_movies"] == 1
    assert summary["failed_movies"] == 0
    assert summary["failed_items"] == []


def test_download_task_service_run_import_job_marks_failure_when_bootstrap_fails(download_tables, tmp_path):
    library = MediaLibrary.create(name="Main", backend="local", backend_config={"root_path": str(tmp_path / "library")})
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


def test_map_remote_path_tolerates_trailing_slash_in_config(download_tables):
    # client_save_path 带尾斜杠、qB 返回不带尾斜杠（刚加完磁链 content_path 为空回退到 save_path）时，
    # 应正确映射而不是误报 422
    library = _create_library()
    client = DownloadClient.create(
        name="client-slash",
        base_url="http://localhost:8080",
        username="alice",
        password="secret",
        client_save_path="/downloads/sakura/",
        local_root_path="/mnt/downloads/sakura/",
        media_library=library,
    )
    # 恰好等于保存目录
    assert map_remote_path(client, "/downloads/sakura") == "/mnt/downloads/sakura/"
    # 子目录映射
    assert map_remote_path(client, "/downloads/sakura/ABC-001") == "/mnt/downloads/sakura/ABC-001"


def test_map_remote_path_rejects_unrelated_path(download_tables):
    library = _create_library()
    client = _create_client(library)  # client_save_path=/downloads/a
    with pytest.raises(ApiError) as exc:
        map_remote_path(client, "/elsewhere/x")
    assert exc.value.code == "invalid_download_client_path_mapping"


def test_add_candidate_handles_duplicate_conflict(download_tables):
    # 重复提交已存在的种子时，torrents_add 抛 Conflict409Error，应幂等处理：补系统标签 + 复用已存在种子
    import qbittorrentapi

    class FakeTorrent:
        hash = "abc123def4567890abc123def4567890abc12345"
        name = "ABC-001"
        progress = 0.0
        state = "queuedDL"
        tags = "sakuramedia, client:1"
        content_path = ""
        save_path = "/downloads/a"

    class FakeApiClient:
        def __init__(self):
            self.added_tags = None

        def auth_log_in(self):
            return None

        def torrents_add(self, **kwargs):
            raise qbittorrentapi.Conflict409Error("Conflict")

        def torrents_add_tags(self, *, tags, torrent_hashes):
            self.added_tags = (tags, torrent_hashes)

        def torrents_info(self, **kwargs):
            return [FakeTorrent()]

    fake = FakeApiClient()
    qb = QBittorrentClient(base_url="http://x", username="u", password="p", client=fake)
    info_hash = "abc123def4567890abc123def4567890abc12345"
    magnet = f"magnet:?xt=urn:btih:{info_hash.upper()}&dn=ABC-001"
    result = qb.add_candidate(
        magnet_url=magnet,
        torrent_url="",
        save_path="/downloads/a",
        rename="ABC-001",
        client_id=1,
    )

    assert fake.added_tags == ("sakuramedia,client:1", info_hash)
    assert result["info_hash"] == info_hash


def test_download_task_service_lists_filters_and_controls_tasks(download_tables):
    library = _create_library()
    first_client = _create_client(library)
    second_client = _create_client(library, name="client-b")
    first = DownloadTask.create(
        client=first_client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path="/mnt/downloads/a/ABC-001",
        progress=0.2,
        download_state="downloading",
        import_status="pending",
    )
    DownloadTask.create(
        client=second_client,
        movie="DEF-002",
        name="DEF-002",
        info_hash="hash-2",
        save_path="/mnt/downloads/a/DEF-002",
        progress=0.0,
        download_state="queued",
        import_status="pending",
    )

    page = DownloadTaskService.list_tasks(client_id=first_client.id, movie_number=" abc-001 ")
    assert page.total == 1
    assert page.items[0].id == first.id

    # 按 download_state 精确筛选：downloading 只匹配 first，queued 只匹配另一条
    downloading_page = DownloadTaskService.list_tasks(download_state="downloading")
    assert [item.id for item in downloading_page.items] == [first.id]
    queued_page = DownloadTaskService.list_tasks(download_state="queued")
    assert [item.movie_number for item in queued_page.items] == ["DEF-002"]
    # 空字符串等价于不过滤
    assert DownloadTaskService.list_tasks(download_state="  ").total == 2
    # 非法值直接抛 422
    with pytest.raises(ApiError) as exc_info:
        DownloadTaskService.list_tasks(download_state="not-a-state")
    assert exc_info.value.code == "invalid_download_task_filter"

    calls = []

    class FakeQBittorrentClient:
        @classmethod
        def from_download_client(cls, download_client):
            return cls()

        def pause_torrent(self, info_hash, *, client_id):
            calls.append(("pause", info_hash, client_id))

        def resume_torrent(self, info_hash, *, client_id):
            calls.append(("resume", info_hash, client_id))

        def delete_torrent(self, info_hash, *, client_id, delete_files):
            calls.append(("delete", info_hash, client_id, delete_files))
            return True

    assert DownloadTaskService.pause_task(first.id, qbittorrent_client_cls=FakeQBittorrentClient).status == "ok"
    assert DownloadTaskService.resume_task(first.id, qbittorrent_client_cls=FakeQBittorrentClient).action == "resume"
    removed = DownloadTaskService.delete_task(
        first.id,
        delete_files=True,
        qbittorrent_client_cls=FakeQBittorrentClient,
    )
    assert removed["task_id"] == first.id
    assert DownloadTask.get_or_none(DownloadTask.id == first.id) is None
    assert calls == [
        ("pause", "hash-1", first_client.id),
        ("resume", "hash-1", first_client.id),
        ("delete", "hash-1", first_client.id, True),
    ]


def test_list_download_tasks_inlines_movie_title_and_cover(download_tables):
    # 让前端下载卡片能直接展示中文标题 + 封面，不再逐张卡片二次查 GET /movies/search/local。
    library = _create_library()
    client = _create_client(library)
    cover = Image.create(
        origin="/files/images/orig.jpg",
        small="/files/images/s.jpg",
        medium="/files/images/m.jpg",
        large="/files/images/l.jpg",
    )
    movie = Movie.create(
        javdb_id="javdb-ABC-001",
        movie_number="ABC-001",
        title="ABC-001 Original",
        title_zh="中文标题",
        cover_image=cover,
        is_subscribed=False,
    )
    DownloadTask.create(
        client=client,
        movie=movie.movie_number,
        name="ABC-001 raw",
        info_hash="hash-with-movie",
        save_path="/downloads/a/ABC-001",
        progress=1.0,
        download_state="seeding",
        import_status="pending",
    )
    # 番号在库里查不到的下载任务：movie_title / movie_cover 应保持 None，前端 fallback 到 task.name
    DownloadTask.create(
        client=client,
        movie="NOEXIST-999",
        name="unknown movie",
        info_hash="hash-no-movie",
        save_path="/downloads/a/NOEXIST-999",
        progress=0.5,
        download_state="downloading",
        import_status="pending",
    )
    # 无番号的下载任务（predownload 场景）：不应把它算进 movie JOIN 的 IN 列表里
    DownloadTask.create(
        client=client,
        movie=None,
        name="raw torrent",
        info_hash="hash-no-number",
        save_path="/downloads/a/raw",
        progress=0.1,
        download_state="downloading",
        import_status="pending",
    )

    page = DownloadTaskService.list_tasks(page=1, page_size=20)
    by_hash = {item.info_hash: item for item in page.items}

    assert by_hash["hash-with-movie"].movie_title == "中文标题"
    assert by_hash["hash-with-movie"].movie_cover is not None
    assert by_hash["hash-with-movie"].movie_cover.origin == "/files/images/orig.jpg"
    assert by_hash["hash-no-movie"].movie_title is None
    assert by_hash["hash-no-movie"].movie_cover is None
    assert by_hash["hash-no-number"].movie_title is None
    assert by_hash["hash-no-number"].movie_cover is None


def test_download_task_service_rejects_delete_while_import_running(download_tables):
    library = _create_library()
    client = _create_client(library)
    task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="hash-1",
        save_path="/mnt/downloads/a/ABC-001",
        progress=1.0,
        download_state="completed",
        import_status="running",
    )

    with pytest.raises(ApiError) as exc_info:
        DownloadTaskService.delete_task(task.id, delete_files=True)
    assert exc_info.value.code == "download_task_import_running"


def test_download_progress_hub_merges_partial_delta_without_exposing_manual_torrent(download_tables):
    library = _create_library()
    client = _create_client(library)
    task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        name="ABC-001",
        info_hash="managed-hash",
        save_path="/mnt/downloads/a/ABC-001",
        progress=0.0,
        download_state="queued",
        import_status="pending",
    )
    hub = DownloadProgressHub()
    subscription = DownloadProgressSubscription(subscription_id=1, client_ids={client.id})
    hub._subscriptions[subscription.subscription_id] = subscription

    hub._consume_delta(
        client.id,
        {
            "full_update": True,
            "torrents": {
                "managed-hash": {
                    "name": "ABC-001",
                    "tags": f"sakuramedia,client:{client.id}",
                    "progress": 0.4,
                    "state": "downloading",
                    "dlspeed": 20,
                    "downloaded": 40,
                    "size": 100,
                    "eta": 3,
                },
                "manual-hash": {
                    "name": "manual",
                    "tags": "manual",
                    "progress": 0.9,
                    "state": "downloading",
                },
            },
            "torrents_removed": [],
            "server_state": {},
        },
    )
    event, payload = subscription.queue.get_nowait()
    assert event == "snapshot"
    assert payload["items"] == [
        {
            "task_id": task.id,
            "client_id": client.id,
            "movie_number": "ABC-001",
            "name": "ABC-001",
            "info_hash": "managed-hash",
            "progress": 0.4,
            "raw_state": "downloading",
            "download_state": "downloading",
            "download_speed_bytes": 20,
            "uploaded_speed_bytes": 0,
            "downloaded_bytes": 40,
            "total_size_bytes": 100,
            "eta_seconds": 3,
        }
    ]
    # 快照后不应紧跟同内容的 download_task_updated，避免客户端重复渲染。
    with pytest.raises(Empty):
        subscription.queue.get_nowait()

    hub._consume_delta(
        client.id,
        {
            "full_update": False,
            "torrents": {"managed-hash": {"progress": 0.5, "dlspeed": 30}},
            "torrents_removed": [],
            "server_state": {},
        },
    )
    event, payload = subscription.queue.get_nowait()
    assert event == "download_task_updated"
    assert payload["progress"] == 0.5
    assert payload["download_speed_bytes"] == 30
    assert payload["name"] == "ABC-001"
