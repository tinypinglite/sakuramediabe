from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.api.exception.errors import ApiError
from src.model import (
    DownloadClient,
    DownloadSubmissionRecord,
    DownloadTask,
    MediaLibrary,
)
from src.plugins.provider_protocol import (
    ConfigField,
    ImportFile,
    ProviderDiagnosticCheck,
    ProviderDiagnosticReport,
    RemoteDownloadTask,
)
from src.schema.transfers.downloads import (
    DownloadClientTestRequest,
    DownloadClientUpdateRequest,
    DownloadRequestCreateRequest,
)
from src.service.transfers.downloads.client_config_service import DownloadClientService
from src.service.transfers.downloads.common import validate_remote_download_task
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.sync_service import (
    REMOTE_MISS_CONFIRM_SECONDS,
    DownloadSyncService,
)
from src.service.transfers.downloads.task_service import DownloadTaskService
from src.service.transfers.imports.import_service import MediaImportService


def test_download_request_requires_candidate_indexer_name():
    with pytest.raises(ValidationError):
        DownloadRequestCreateRequest(
            movie_number="ABC-001",
            candidate={
                "source_uri": "https://example.test/item",
                "title": "ABC-001",
                "size_bytes": 1,
                "seeders": 1,
            },
        )


def test_download_request_resolves_only_clients_bound_to_candidate_indexer(monkeypatch):
    indexer = SimpleNamespace(id=1, name="indexer")
    preferred = SimpleNamespace(id=2)
    unbound = SimpleNamespace(id=3)
    payload = DownloadRequestCreateRequest(
        movie_number="ABC-001",
        candidate={
            "source_uri": "https://example.test/item",
            "indexer_name": "indexer",
            "title": "ABC-001",
            "size_bytes": 1,
            "seeders": 1,
        },
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.request_service.require_indexer",
        lambda _name: indexer,
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.request_service.list_indexer_clients",
        lambda _indexer: [preferred],
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.request_service.resolve_preferred_client",
        lambda _clients: preferred,
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.request_service.require_client",
        lambda _client_id: unbound,
    )

    assert DownloadRequestService._resolve_client(payload) is preferred

    with pytest.raises(ApiError) as error:
        DownloadRequestService._resolve_client(payload.model_copy(update={"client_id": unbound.id}))
    assert error.value.code == "download_request_client_not_bound_to_indexer"


def test_invalid_provider_download_result_is_rejected():
    with pytest.raises(ApiError) as error:
        validate_remote_download_task(SimpleNamespace(remote_id="remote"))
    assert error.value.code == "provider_invalid_response"


def test_cleared_completed_source_ref_is_rejected():
    task = RemoteDownloadTask(
        remote_id="remote",
        name="name",
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
    )
    task.completed_source_ref.clear()
    with pytest.raises(ApiError) as error:
        validate_remote_download_task(task)
    assert error.value.code == "provider_invalid_response"


@pytest.mark.parametrize("name", ["", ".", "..", "folder/file.mp4", r"folder\file.mp4"])
def test_provider_import_file_name_must_be_one_safe_segment(name):
    source = ImportFile(
        source_ref={"id": "1"},
        name=name,
        relative_path=name,
        size_bytes=1,
        is_video=True,
    )
    with pytest.raises(ApiError) as error:
        MediaImportService._validate_import_file(source)
    assert error.value.code == "provider_invalid_response"


def test_provider_import_file_title_does_not_strip_path():
    source = ImportFile(
        source_ref={"id": "1"},
        name="movie.mp4",
        relative_path="opaque",
        size_bytes=1,
        is_video=True,
    )
    assert MediaImportService._title_for(source) == "movie"


@pytest.mark.parametrize(
    "size_bytes,is_video",
    [(-1, True), (True, True), (1, "yes")],
)
def test_provider_import_file_metadata_must_be_valid(size_bytes, is_video):
    source = ImportFile(
        source_ref={"id": "1"},
        name="movie.mp4",
        relative_path="opaque",
        size_bytes=size_bytes,
        is_video=is_video,
    )
    with pytest.raises(ApiError) as error:
        MediaImportService._validate_import_file(source)
    assert error.value.code == "provider_invalid_response"


def test_download_client_resource_hides_config_without_download_component(monkeypatch):
    client = SimpleNamespace(
        id=1,
        name="client",
        library_id=1,
        provider_config={"cookie": "secret"},
        created_at=datetime.now(),
        updated_at=datetime.now(),
        library=SimpleNamespace(provider_key="storage-only"),
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.client_config_service.MEDIA_PROVIDER_REGISTRY.require",
        lambda _provider_key: SimpleNamespace(downloads=None),
    )
    assert DownloadClientService._resource(client).provider_config == {}


def test_read_only_download_config_is_rejected():
    bundle = SimpleNamespace(
        downloads=SimpleNamespace(
            config_fields=(
                ConfigField(
                    key="account",
                    label="Account",
                    input="text",
                    required=True,
                    read_only=True,
                ),
            )
        )
    )
    with pytest.raises(ApiError) as error:
        DownloadClientService._validate_config(bundle, {"account": "provider"})
    assert error.value.code == "invalid_download_client_provider_config"


def test_download_client_update_rejects_null_provider_config(monkeypatch):
    monkeypatch.setattr(
        "src.service.transfers.downloads.client_config_service.require_client",
        lambda _client_id: SimpleNamespace(library=SimpleNamespace()),
    )
    monkeypatch.setattr(DownloadClientService, "_bundle", lambda _library: SimpleNamespace())

    with pytest.raises(ApiError) as error:
        DownloadClientService.update_client(1, DownloadClientUpdateRequest(provider_config=None))

    assert error.value.status_code == 422
    assert error.value.code == "invalid_download_client_provider_config"


def test_download_client_test_prepares_secret_and_returns_warning(monkeypatch):
    library = SimpleNamespace(
        id=1,
        provider_key="demo",
        provider_config={},
        account_key=None,
    )
    existing = SimpleNamespace(
        id=7,
        library_id=1,
        library=library,
        provider_config={"url": "https://old.example", "password": "saved"},
    )
    prepared_config = {}

    def prepare_client(*, submitted_config, library, previous):
        prepared_config.update(submitted_config)
        return submitted_config

    bundle = SimpleNamespace(
        downloads=SimpleNamespace(
            config_fields=(
                ConfigField(key="url", label="URL", input="text", required=True),
                ConfigField(key="password", label="Password", input="secret", required=True),
            ),
            prepare_client=prepare_client,
            test_client=lambda *, submitted_config, library: ProviderDiagnosticReport(
                status="warning",
                checks=(
                    ProviderDiagnosticCheck(
                        key="hardlink",
                        status="warning",
                        code="hardlink_unavailable",
                        message="硬链接不可用",
                    ),
                ),
            ),
        )
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.client_config_service.require_library",
        lambda _library_id: library,
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.client_config_service.require_client",
        lambda _client_id: existing,
    )
    monkeypatch.setattr(DownloadClientService, "_bundle", lambda _library: bundle)

    result = DownloadClientService.test_client(
        DownloadClientTestRequest(
            client_id=7,
            library_id=1,
            provider_config={"url": "https://new.example"},
        )
    )

    assert prepared_config == {"url": "https://new.example", "password": "saved"}
    assert result.status == "warning"
    assert result.checks[0].code == "hardlink_unavailable"
    assert result.elapsed_ms >= 0


def test_empty_download_snapshot_does_not_remove_ghost_tasks(test_db):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    DownloadTask.create(
        client=client,
        remote_id="ghost",
        name="ghost",
        state="failed",
        progress=0,
        import_status="pending",
    )
    pending = DownloadTask.create(
        client=client,
        remote_id="pending",
        name="pending",
        state="completed",
        progress=1,
        completed_source_ref={"id": "pending-source"},
        import_status="pending",
    )
    running = DownloadTask.create(
        client=client,
        remote_id="running",
        name="running",
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
        import_status="running",
    )

    result = DownloadSyncService(provider_factory=lambda _client: SimpleNamespace(list_tasks=lambda: ())).sync_client(
        client.id
    )
    assert result.removed_count == 0
    assert DownloadTask.get_or_none(DownloadTask.remote_id == "ghost") is not None
    assert DownloadTask.get_or_none(DownloadTask.id == pending.id) is not None
    assert DownloadTask.get_or_none(DownloadTask.id == running.id) is not None
    assert DownloadSyncService._prune_ghost_tasks(client.id, set()) == 0


def test_download_sync_only_updates_movie_linked_tasks(test_db):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    long_remote_name = "long-" + "n" * 400
    known_task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        remote_id="known",
        name="old-name",
        state="queued",
        progress=0,
        import_status="pending",
    )
    external_task = DownloadTask.create(
        client=client,
        remote_id="external",
        name="old-external-name",
        state="queued",
        progress=0,
        import_status="pending",
    )
    remote_tasks = (
        RemoteDownloadTask(
            remote_id="known",
            name=long_remote_name,
            state="downloading",
            progress=0.5,
            completed_source_ref=None,
        ),
        RemoteDownloadTask(
            remote_id="external",
            name="new-external-name",
            state="downloading",
            progress=0.5,
            completed_source_ref=None,
        ),
        RemoteDownloadTask(
            remote_id="unregistered",
            name="unregistered",
            state="queued",
            progress=0,
            completed_source_ref=None,
        ),
    )

    result = DownloadSyncService(
        provider_factory=lambda _client: SimpleNamespace(list_tasks=lambda: remote_tasks)
    ).sync_client(client.id)

    known_task = DownloadTask.get_by_id(known_task.id)
    assert known_task.name == long_remote_name
    assert known_task.state == "downloading"
    assert known_task.progress == 0.5
    external_task = DownloadTask.get_by_id(external_task.id)
    assert external_task.name == "old-external-name"
    assert external_task.state == "queued"
    assert DownloadTask.get_or_none(DownloadTask.remote_id == "unregistered") is None
    assert result.created_count == 0
    assert result.updated_count == 1
    assert result.unchanged_count == 2


def test_download_sync_rejects_orphan_without_submitted_record(test_db):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    info_hash = "b" * 40
    DownloadSubmissionRecord.create(
        client_id=client.id,
        movie_number="ABC-002",
        indexer_name="indexer",
        title="ABC-002 title",
        source_uri=f"magnet:?xt=urn:btih:{info_hash}",
        info_hash=info_hash,
        state="failed",
        remote_id=info_hash,
    )
    remote_tasks = (
        RemoteDownloadTask(
            remote_id=info_hash,
            name="ABC-002-bbbbbb",
            state="downloading",
            progress=0.25,
            completed_source_ref=None,
        ),
    )

    result = DownloadSyncService(
        provider_factory=lambda _client: SimpleNamespace(list_tasks=lambda: remote_tasks)
    ).sync_client(client.id)

    assert DownloadTask.get_or_none(DownloadTask.remote_id == info_hash) is None
    assert result.created_count == 0
    assert result.unchanged_count == 1


def test_download_sync_never_readopts_submitted_record(test_db):
    """自愈退役后的契约：即使存在 submitted 提交记录，远端任务也不会重建台账。"""
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    info_hash = "a" * 40
    DownloadSubmissionRecord.create(
        client_id=client.id,
        movie_number="ABC-001",
        indexer_name="indexer",
        title="ABC-001 title",
        source_uri=f"magnet:?xt=urn:btih:{info_hash}",
        info_hash=info_hash,
        state="submitted",
        remote_id=info_hash,
    )
    remote_tasks = (
        RemoteDownloadTask(
            remote_id=info_hash,
            name="ABC-001-aaaaaa",
            state="downloading",
            progress=0.25,
            completed_source_ref=None,
        ),
    )

    result = DownloadSyncService(
        provider_factory=lambda _client: SimpleNamespace(list_tasks=lambda: remote_tasks)
    ).sync_client(client.id)

    assert DownloadTask.get_or_none(DownloadTask.remote_id == info_hash) is None
    assert result.created_count == 0
    assert result.unchanged_count == 1


def test_deleted_task_stays_deleted_when_provider_lags(test_db, monkeypatch):
    """宿主删除后远端快照仍含该任务（删除可见性延迟），也不会被重建。"""
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    info_hash = "c" * 40
    task = DownloadTask.create(
        client=client,
        movie="ABC-005",
        remote_id=info_hash,
        name="ABC-005-cccccc",
        state="downloading",
        progress=0.1,
        import_status="pending",
    )
    monkeypatch.setattr(
        "src.service.transfers.downloads.task_service.download_provider",
        lambda _client: SimpleNamespace(delete_task=lambda **_kwargs: None),
    )

    DownloadTaskService.delete_task(task.id, delete_files=False)

    assert DownloadTask.get_or_none(DownloadTask.id == task.id) is None

    remote_tasks = (
        RemoteDownloadTask(
            remote_id=info_hash,
            name="ABC-005-cccccc",
            state="downloading",
            progress=0.1,
            completed_source_ref=None,
        ),
    )
    result = DownloadSyncService(
        provider_factory=lambda _client: SimpleNamespace(list_tasks=lambda: remote_tasks)
    ).sync_client(client.id)

    assert result.created_count == 0
    assert DownloadTask.get_or_none(DownloadTask.remote_id == info_hash) is None


def test_download_sync_refreshes_remote_seen_at(test_db):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    task = DownloadTask.create(
        client=client,
        movie="ABC-003",
        remote_id="known",
        name="known",
        state="queued",
        progress=0,
        import_status="pending",
        remote_seen_at=datetime(2020, 1, 1),
    )
    remote_tasks = (
        RemoteDownloadTask(
            remote_id="known",
            name="known",
            state="downloading",
            progress=0.5,
            completed_source_ref=None,
        ),
    )

    result = DownloadSyncService(
        provider_factory=lambda _client: SimpleNamespace(list_tasks=lambda: remote_tasks)
    ).sync_client(client.id)

    refreshed = DownloadTask.get_by_id(task.id)
    assert refreshed.remote_seen_at > datetime(2020, 1, 1)
    assert result.updated_count == 1


def test_prune_ghost_tasks_requires_confirmed_absence(test_db):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    now = datetime(2026, 10, 7, 12, 0, 0)

    def make(remote_id, *, seen_at=None, created_at=None, **overrides):
        values = {
            "client": client,
            "remote_id": remote_id,
            "name": remote_id,
            "state": "downloading",
            "progress": 0,
            "import_status": "pending",
            "remote_seen_at": seen_at,
            "created_at": created_at or now,
        }
        values.update(overrides)
        return DownloadTask.create(**values)

    keep_recent_seen = make("recent-seen", seen_at=now - timedelta(minutes=2))
    prune_stale_seen = make("stale-seen", seen_at=now - timedelta(minutes=10))
    keep_unseen_fresh = make("unseen-fresh", created_at=now - timedelta(minutes=5))
    prune_unseen_old = make("unseen-old", created_at=now - timedelta(minutes=30))
    prune_queued_stale = make(
        "queued-stale", state="queued", seen_at=now - timedelta(minutes=10)
    )
    keep_exact_cutoff = make(
        "exact-cutoff",
        seen_at=now - timedelta(seconds=REMOTE_MISS_CONFIRM_SECONDS),
    )
    keep_in_snapshot = make("in-snapshot", seen_at=now - timedelta(hours=2))
    # 导入在途（非终态 + running）绝不删除；终态行不参与对账。
    keep_running = make(
        "running",
        seen_at=now - timedelta(hours=2),
        import_status="running",
    )
    keep_completed_running = make(
        "completed-running",
        seen_at=now - timedelta(hours=2),
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
        import_status="running",
    )
    keep_importable = make(
        "importable",
        seen_at=now - timedelta(hours=2),
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
        import_status="failed",
    )
    keep_skipped = make(
        "skipped",
        seen_at=now - timedelta(hours=2),
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
        import_status="skipped",
    )
    keep_imported = make(
        "imported",
        seen_at=now - timedelta(hours=2),
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
        import_status="completed",
    )
    keep_download_failed = make(
        "download-failed",
        seen_at=now - timedelta(hours=2),
        state="failed",
        import_status="pending",
    )

    removed = DownloadSyncService._prune_ghost_tasks(
        client.id, {"in-snapshot"}, now=now
    )

    assert removed == 3
    assert DownloadTask.get_or_none(DownloadTask.id == prune_stale_seen.id) is None
    assert DownloadTask.get_or_none(DownloadTask.id == prune_unseen_old.id) is None
    assert DownloadTask.get_or_none(DownloadTask.id == prune_queued_stale.id) is None
    for kept in (
        keep_recent_seen,
        keep_unseen_fresh,
        keep_exact_cutoff,
        keep_in_snapshot,
        keep_running,
        keep_completed_running,
        keep_importable,
        keep_skipped,
        keep_imported,
        keep_download_failed,
    ):
        assert DownloadTask.get_or_none(DownloadTask.id == kept.id) is not None


def test_imported_task_survives_remote_disappearance(test_db):
    """回归：导入成功的终态行不因远端快照缺席被清理。"""
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    imported = DownloadTask.create(
        client=client,
        movie="ABC-010",
        remote_id="imported-remote",
        name="ABC-010-imported",
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
        import_status="completed",
        remote_seen_at=datetime(2020, 1, 1),
    )
    active = DownloadTask.create(
        client=client,
        movie="ABC-011",
        remote_id="active-remote",
        name="ABC-011-active",
        state="downloading",
        progress=0.5,
        import_status="pending",
        remote_seen_at=datetime(2020, 1, 1),
    )
    remote_tasks = (
        RemoteDownloadTask(
            remote_id="active-remote",
            name="ABC-011-active",
            state="downloading",
            progress=0.6,
            completed_source_ref=None,
        ),
    )

    result = DownloadSyncService(
        provider_factory=lambda _client: SimpleNamespace(list_tasks=lambda: remote_tasks)
    ).sync_client(client.id)

    assert result.removed_count == 0
    assert DownloadTask.get_or_none(DownloadTask.id == imported.id) is not None
    assert DownloadTask.get_by_id(active.id).progress == 0.6


def test_auto_import_skips_unbound_completed_tasks(test_db, monkeypatch):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    tracked_task = DownloadTask.create(
        client=client,
        movie="ABC-001",
        remote_id="tracked",
        name="tracked",
        state="completed",
        progress=1,
        completed_source_ref={"id": "tracked-source"},
        import_status="pending",
    )
    DownloadTask.create(
        client=client,
        remote_id="external",
        name="external",
        state="completed",
        progress=1,
        completed_source_ref={"id": "external-source"},
        import_status="pending",
    )
    triggered_batches: list[list[int]] = []
    monkeypatch.setattr(
        "src.service.transfers.downloads.sync_service.ImportTaskService.enqueue_batch",
        lambda tasks, **_kwargs: triggered_batches.append([task.id for task in tasks]),
    )

    result = DownloadSyncService().enqueue_auto_imports()

    assert triggered_batches == [[tracked_task.id]]
    assert result["queued_count"] == 1


def test_auto_import_batches_tasks_for_each_library(test_db, monkeypatch):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    client = DownloadClient.create(name="client", library=library, provider_config={})
    first = DownloadTask.create(
        client=client,
        movie="ABC-001",
        remote_id="first",
        name="first",
        state="completed",
        progress=1,
        completed_source_ref={"id": "first-source"},
        import_status="pending",
    )
    second = DownloadTask.create(
        client=client,
        movie="ABC-002",
        remote_id="second",
        name="second",
        state="completed",
        progress=1,
        completed_source_ref={"id": "second-source"},
        import_status="pending",
    )
    triggered_batches: list[list[int]] = []
    monkeypatch.setattr(
        "src.service.transfers.downloads.sync_service.ImportTaskService.enqueue_batch",
        lambda tasks, **_kwargs: triggered_batches.append([task.id for task in tasks]),
    )

    result = DownloadSyncService().enqueue_auto_imports()

    assert triggered_batches == [[first.id, second.id]]
    assert result["queued_count"] == 2


def test_download_sync_skips_clients_without_active_tasks(test_db):
    library = MediaLibrary.create(name="library", provider_key="demo", provider_config={})
    idle_client = DownloadClient.create(name="idle", library=library, provider_config={})
    completed_client = DownloadClient.create(name="completed", library=library, provider_config={})
    active_client = DownloadClient.create(name="active", library=library, provider_config={})
    DownloadTask.create(
        client=completed_client,
        remote_id="completed",
        name="completed",
        state="completed",
        progress=1,
        completed_source_ref={"id": "source"},
        import_status="pending",
    )
    DownloadTask.create(
        client=active_client,
        remote_id="active",
        name="active",
        state="queued",
        progress=0,
        import_status="pending",
    )
    queried_client_ids: list[int] = []

    def provider(client):
        queried_client_ids.append(client.id)
        return SimpleNamespace(list_tasks=lambda: ())

    result = DownloadSyncService(provider_factory=provider).sync_all_clients()

    assert queried_client_ids == [active_client.id]
    assert result["total_clients"] == 1
    assert idle_client.id not in queried_client_ids
    assert completed_client.id not in queried_client_ids
