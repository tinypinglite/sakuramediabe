import asyncio
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from src.lib.cloud115 import DirBreadcrumb, DirEntry, DirMeta
from src.service.transfers.cloud115_import_common import (
    Cloud115TargetDirCache,
    Cloud115TargetDirResolver,
    collect_cloud115_source_tree,
)
from src.service.transfers.cloud115_import_service import (
    Cloud115ImportService,
    CloudImportGroup,
)
from src.service.transfers.cloud115_import_job_service import (
    Cloud115ImportJobService,
)
from src.service.transfers.cloud115_offline_sync_service import (
    Cloud115OfflineSyncService,
)


def _entry(
    entry_id: str,
    parent_id: str,
    name: str,
    *,
    is_dir: bool,
) -> DirEntry:
    return DirEntry(
        entry_id=entry_id,
        parent_id=parent_id,
        name=name,
        is_dir=is_dir,
        size=0 if is_dir else 1024,
        sha1=None if is_dir else f"SHA-{entry_id}",
        pickcode="",
        mtime=0,
        ctime=0,
        is_video=not is_dir,
    )


def test_collect_source_tree_lists_each_directory_once():
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def list_dir(self, cid, *, offset, limit):
            self.calls.append((cid, offset, limit))
            entries = {
                "source": [
                    _entry("sub", "source", "CD1", is_dir=True),
                    _entry("file-1", "source", "ABC-001.mp4", is_dir=False),
                ],
                "sub": [
                    _entry("file-2", "sub", "ABC-001-CD1.mp4", is_dir=False),
                ],
            }[cid]
            return entries, len(entries)

    client = FakeClient()
    dir_map, files = asyncio.run(collect_cloud115_source_tree(client, "source"))

    assert client.calls == [
        ("source", 0, 1150),
        ("sub", 0, 1150),
    ]
    assert dir_map == {"sub": ("CD1", "source")}
    assert [item.entry_id for item in files] == ["file-1", "file-2"]


def test_target_directory_cache_is_reused_across_import_jobs():
    cache = Cloud115TargetDirCache()

    class FakeClient:
        def __init__(self, listings):
            self.listings = listings
            self.list_calls = []
            self.mkdir_calls = []

        async def list_dir(self, cid, *, offset, limit):
            self.list_calls.append((cid, offset, limit))
            entries = self.listings.get(cid, [])
            return entries, len(entries)

        async def mkdir(self, parent_cid, name):
            self.mkdir_calls.append((parent_cid, name))
            return f"created-{name}"

    first_client = FakeClient(
        {
            "root": [_entry("jav-cid", "root", "jav", is_dir=True)],
            "jav-cid": [_entry("old-cid", "jav-cid", "OLD-001", is_dir=True)],
        }
    )
    first_resolver = Cloud115TargetDirResolver(
        first_client,
        root_cid="root",
        cache=cache,
    )
    assert asyncio.run(first_resolver.resolve_jav_entity("OLD-001")) == (
        "old-cid",
        False,
    )
    assert len(first_client.list_calls) == 2

    # 下一条自动导入使用新的 SDK client，但复用同一批次目录缓存。
    second_client = FakeClient({})
    second_resolver = Cloud115TargetDirResolver(
        second_client,
        root_cid="root",
        cache=cache,
    )
    assert asyncio.run(second_resolver.resolve_jav_entity("NEW-002")) == (
        "created-NEW-002",
        True,
    )
    assert asyncio.run(
        second_resolver.create_version_dir(
            entity_cid="created-NEW-002",
            now_ms=1700000000000,
        )
    ) == "created-1700000000000"
    assert second_client.list_calls == []
    assert second_client.mkdir_calls == [
        ("jav-cid", "NEW-002"),
        ("created-NEW-002", "1700000000000"),
    ]


def test_managed_import_uses_one_source_metadata_request(monkeypatch):
    source_meta = DirMeta(
        cid="source",
        name="ABC-001",
        pickcode="",
        parent_id="download-root",
        file_count=0,
        folder_count=0,
        play_long_seconds=0,
        mtime=0,
        ctime=0,
        paths=(DirBreadcrumb(file_id="download-root", name="downloads"),),
    )
    client = SimpleNamespace(dir_info=AsyncMock(return_value=source_meta))

    @asynccontextmanager
    async def fake_client_for(_library):
        yield client

    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.cloud115_client_for",
        fake_client_for,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.require_cloud115_library",
        lambda _library: {
            "root_cid": "library-root",
            "download_root_cid": "download-root",
        },
    )
    service = Cloud115ImportService()
    service._scan_source = AsyncMock(return_value=([], 0, 0))
    job = SimpleNamespace(source_path="", save=lambda: None)

    asyncio.run(
        service._run(
            library=object(),
            source_cid="source",
            transfer_mode="cleanup-source",
            only_files=None,
            failure_items=[],
            stats={"imported": 0, "skipped": 0, "failed": 0},
            new_playable_movies={},
            progress_callback=None,
            job=job,
            managed_download_source=True,
            target_dir_cache=Cloud115TargetDirCache(),
        )
    )

    client.dir_info.assert_awaited_once_with("source")
    assert job.source_path == "downloads/ABC-001"


def test_manual_trigger_reuses_source_metadata_from_safety_check(monkeypatch):
    source_meta = SimpleNamespace(name="ABC-001")
    client = SimpleNamespace(dir_info=AsyncMock())
    safety_check = AsyncMock(return_value=source_meta)

    @asynccontextmanager
    async def fake_client_for(_library):
        yield client

    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_job_service.cloud115_client_for",
        fake_client_for,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_job_service.require_cloud115_library",
        lambda _library: {"root_cid": "library-root"},
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_job_service.assert_cid_outside_library_root",
        safety_check,
    )

    source_name = Cloud115ImportJobService._validate_source_and_fetch_name(
        object(),
        "source",
    )

    assert source_name == "ABC-001"
    safety_check.assert_awaited_once_with(
        client,
        source_cid="source",
        root_cid="library-root",
    )
    client.dir_info.assert_not_awaited()


def _run_import_groups(
    monkeypatch,
    *,
    managed_download_source: bool,
    count: int,
    failed_movie_ids: set[int] | None = None,
):
    source_meta = DirMeta(
        cid="source",
        name="batch",
        pickcode="",
        parent_id="download-root",
        file_count=0,
        folder_count=0,
        play_long_seconds=0,
        mtime=0,
        ctime=0,
        paths=(DirBreadcrumb(file_id="download-root", name="downloads"),),
    )
    client = SimpleNamespace(dir_info=AsyncMock(return_value=source_meta))
    safety_check = AsyncMock(return_value=source_meta)

    @asynccontextmanager
    async def fake_client_for(_library):
        yield client

    groups = [
        CloudImportGroup(movie_number=f"TEST-{index:03d}")
        for index in range(1, count + 1)
    ]
    failed_movie_ids = failed_movie_ids or set()

    def metadata_result(movie_id):
        failed = movie_id in failed_movie_ids
        return SimpleNamespace(
            failure_reason="metadata_failed" if failed else None,
            failure_detail="failed" if failed else None,
            movie_id=movie_id,
        )

    @contextmanager
    def metadata_import_batch(movie_numbers, *, thread_name_prefix):
        del thread_name_prefix
        yield {
            movie_number: SimpleNamespace(
                result=lambda movie_id=index: metadata_result(movie_id)
            )
            for index, movie_number in enumerate(movie_numbers, start=1)
        }

    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.cloud115_client_for",
        fake_client_for,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.require_cloud115_library",
        lambda _library: {
            "root_cid": "library-root",
            "download_root_cid": "download-root",
        },
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.assert_cid_outside_library_root",
        safety_check,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.Movie.get_by_id",
        lambda movie_id: SimpleNamespace(
            id=movie_id,
            movie_number=f"TEST-{movie_id:03d}",
            title="",
        ),
    )

    delays = iter((11.0, 29.0))
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.random.uniform",
        lambda minimum, maximum: next(delays),
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.asyncio.sleep",
        sleep_mock,
    )

    service = Cloud115ImportService(
        media_import_service=SimpleNamespace(
            metadata_import_batch=metadata_import_batch,
        )
    )
    service._scan_source = AsyncMock(return_value=(groups, 0, 0))
    service._import_group = AsyncMock()
    events = []
    job = SimpleNamespace(id=42, source_path="", save=lambda: None)
    shared_cache = Cloud115TargetDirCache() if managed_download_source else None

    asyncio.run(
        service._run(
            library=object(),
            source_cid="source",
            transfer_mode="cleanup-source",
            only_files=None,
            failure_items=[],
            stats={"imported": 0, "skipped": 0, "failed": 0},
            new_playable_movies={},
            progress_callback=events.append,
            job=job,
            managed_download_source=managed_download_source,
            target_dir_cache=shared_cache,
        )
    )
    return service, safety_check, sleep_mock, events, shared_cache


def test_manual_import_rests_only_before_later_movie_groups(monkeypatch):
    service, safety_check, sleep_mock, events, _ = _run_import_groups(
        monkeypatch,
        managed_download_source=False,
        count=3,
    )

    assert sleep_mock.await_args_list == [call(11.0), call(29.0)]
    waiting_events = [event for event in events if event["event"] == "movie_waiting"]
    assert [event["movie_number"] for event in waiting_events] == [
        "TEST-002",
        "TEST-003",
    ]
    assert [event["completed_movies"] for event in waiting_events] == [1, 2]
    assert "11.0 秒后继续" in waiting_events[0]["text"]
    assert "29.0 秒后继续" in waiting_events[1]["text"]
    safety_check.assert_awaited_once()

    resolver_ids = {
        id(awaited.kwargs["target_dir_resolver"])
        for awaited in service._import_group.await_args_list
    }
    assert len(resolver_ids) == 1
    resolver = service._import_group.await_args_list[0].kwargs[
        "target_dir_resolver"
    ]
    assert isinstance(resolver._cache, Cloud115TargetDirCache)


def test_manual_import_rests_before_next_group_after_ordinary_failure(
    monkeypatch,
):
    service, _, sleep_mock, _, _ = _run_import_groups(
        monkeypatch,
        managed_download_source=False,
        count=2,
        failed_movie_ids={1},
    )

    sleep_mock.assert_awaited_once_with(11.0)
    service._import_group.assert_awaited_once()
    assert (
        service._import_group.await_args.kwargs["group"].movie_number
        == "TEST-002"
    )


def test_single_manual_import_and_managed_import_do_not_use_group_rest(monkeypatch):
    _, _, manual_sleep, _, _ = _run_import_groups(
        monkeypatch,
        managed_download_source=False,
        count=1,
    )
    manual_sleep.assert_not_awaited()

    _, managed_safety, managed_sleep, _, shared_cache = _run_import_groups(
        monkeypatch,
        managed_download_source=True,
        count=3,
    )
    managed_sleep.assert_not_awaited()
    managed_safety.assert_not_awaited()
    assert shared_cache is not None


def test_new_entity_skips_reconciliation_but_existing_entity_keeps_it(
    monkeypatch,
):
    entity_scan = AsyncMock(return_value={})
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.list_cloud115_entity_target_files",
        entity_scan,
    )
    service = Cloud115ImportService()
    group = CloudImportGroup(movie_number="TEST-001")
    common_kwargs = {
        "client": object(),
        "library": object(),
        "movie": object(),
        "group": group,
        "transfer_mode": "cleanup-source",
        "failure_items": [],
        "stats": {"imported": 0, "skipped": 0, "failed": 0},
        "new_playable_movies": {},
    }

    created_resolver = SimpleNamespace(
        resolve_jav_entity=AsyncMock(return_value=("new-cid", True)),
    )
    asyncio.run(
        service._import_group(
            target_dir_resolver=created_resolver,
            **common_kwargs,
        )
    )
    entity_scan.assert_not_awaited()

    existing_resolver = SimpleNamespace(
        resolve_jav_entity=AsyncMock(return_value=("existing-cid", False)),
    )
    asyncio.run(
        service._import_group(
            target_dir_resolver=existing_resolver,
            **common_kwargs,
        )
    )
    entity_scan.assert_awaited_once_with(
        common_kwargs["client"],
        "existing-cid",
    )


def test_auto_import_queue_rests_only_between_successful_jobs(monkeypatch):
    first = SimpleNamespace(id=1, import_status="pending")
    second = SimpleNamespace(id=2, import_status="pending")
    tasks = [first, second]
    client = SimpleNamespace(id=9, media_library_id=7)
    triggered = []
    cache_ids = []
    sleeps = []

    def next_pending(_client_id):
        return next(
            (task for task in tasks if task.import_status == "pending"),
            None,
        )

    def trigger(task, *, target_dir_cache):
        task.import_status = "running"
        triggered.append(task.id)
        cache_ids.append(id(target_dir_cache))
        return SimpleNamespace(import_job_id=task.id)

    def reconcile(task):
        task.import_status = "completed"

    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_next_pending_import",
        staticmethod(next_pending),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_trigger_import",
        staticmethod(trigger),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_wait_for_import_job",
        staticmethod(
            lambda job_id: SimpleNamespace(
                id=job_id,
                state="completed",
                failed_count=0,
            )
        ),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_reconcile_running_import",
        staticmethod(reconcile),
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_offline_sync_service.random.uniform",
        lambda minimum, maximum: 17.5,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_offline_sync_service.time.sleep",
        sleeps.append,
    )

    count = Cloud115OfflineSyncService._drain_pending_imports(client)

    assert count == 2
    assert triggered == [1, 2]
    assert len(set(cache_ids)) == 1
    assert sleeps == [17.5]


def test_auto_import_queue_stops_without_rest_after_failure(monkeypatch):
    first = SimpleNamespace(id=1, import_status="pending")
    second = SimpleNamespace(id=2, import_status="pending")
    tasks = [first, second]
    client = SimpleNamespace(id=9, media_library_id=7)
    triggered = []
    sleeps = []

    def next_pending(_client_id):
        return next(
            (task for task in tasks if task.import_status == "pending"),
            None,
        )

    def trigger(task, *, target_dir_cache):
        task.import_status = "running"
        triggered.append(task.id)
        return SimpleNamespace(import_job_id=task.id)

    def reconcile(task):
        task.import_status = "failed"

    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_next_pending_import",
        staticmethod(next_pending),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_trigger_import",
        staticmethod(trigger),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_wait_for_import_job",
        staticmethod(
            lambda job_id: SimpleNamespace(
                id=job_id,
                state="failed",
                failed_count=1,
            )
        ),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_reconcile_running_import",
        staticmethod(reconcile),
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_offline_sync_service.time.sleep",
        sleeps.append,
    )

    count = Cloud115OfflineSyncService._drain_pending_imports(client)

    assert count == 1
    assert triggered == [1]
    assert sleeps == []
