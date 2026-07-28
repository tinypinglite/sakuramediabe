import asyncio
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from src.lib.cloud115 import DirBreadcrumb, DirEntry, DirMeta
from src.service.transfers.cloud115_import_common import (
    Cloud115TargetDirCache,
    Cloud115TargetDirResolver,
    collect_cloud115_source_files,
)
from src.service.transfers.cloud115_import_service import (
    Cloud115ImportService,
    CloudImportGroup,
    CloudSourceFile,
)
from src.service.transfers.cloud115_import_job_service import (
    Cloud115ImportJobService,
)
from src.service.transfers.cloud115_offline_sync_service import (
    Cloud115OfflineSyncService,
)
from src.service.videos.cloud115_video_import_service import (
    Cloud115VideoImportService,
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


class _FakeSourceTreeClient:
    """按目录树建模的假 client：递归枚举一次性返回全部文件，list_dir 只列一层。"""

    def __init__(self, *, files, children_by_dir, breadcrumbs=None):
        self._files = files
        self._children_by_dir = children_by_dir
        self._breadcrumbs = breadcrumbs or {}
        self.recursive_calls = []
        self.list_calls = []
        self.dir_info_calls = []

    async def iter_files_recursive(self, cid, **kwargs):
        self.recursive_calls.append(cid)
        for entry in self._files:
            yield entry

    async def list_dir(self, cid, *, offset, limit):
        self.list_calls.append((cid, offset, limit))
        entries = self._children_by_dir.get(cid, [])
        return entries, len(entries)

    async def dir_info(self, cid):
        self.dir_info_calls.append(cid)
        name, crumbs = self._breadcrumbs[cid]
        return DirMeta(
            cid=cid,
            name=name,
            pickcode="",
            parent_id=crumbs[-1].file_id if crumbs else "",
            file_count=0,
            folder_count=0,
            play_long_seconds=0,
            mtime=0,
            ctime=0,
            paths=tuple(crumbs),
        )


def _is_video(entry):
    return entry.name.endswith(".mp4")


def test_source_scan_resolves_only_direct_parents_without_touching_empty_dirs():
    """扁平缓冲区：一次递归 + 一次层列举即可，空目录一次都不碰。"""
    files = [
        _entry("file-1", "task-a", "ABC-001.mp4", is_dir=False),
        _entry("file-2", "task-b", "ABC-002.mp4", is_dir=False),
    ]
    children = {
        "source": [
            _entry("task-a", "source", "ABC-001", is_dir=True),
            _entry("task-b", "source", "ABC-002", is_dir=True),
            # 历史残留空目录：不含视频，绝不该被访问。
            _entry("empty-1", "source", "old-task-1", is_dir=True),
            _entry("empty-2", "source", "old-task-2", is_dir=True),
        ]
    }
    client = _FakeSourceTreeClient(files=files, children_by_dir=children)

    entries, rel_dirs = asyncio.run(
        collect_cloud115_source_files(client, "source", needs_rel_path=_is_video)
    )

    assert client.recursive_calls == ["source"]
    assert client.list_calls == [("source", 0, 1150)]
    # 关键：空目录不产生任何 dir_info 请求，成本不随历史任务目录数增长。
    assert client.dir_info_calls == []
    assert [item.entry_id for item in entries] == ["file-1", "file-2"]
    assert rel_dirs["task-a"] == ("ABC-001",)
    assert rel_dirs["task-b"] == ("ABC-002",)


def test_source_scan_rebuilds_full_rel_path_for_nested_dirs():
    """深层目录用一次 dir_info 的面包屑还原完整相对链，rel_path 与旧 BFS 逐字节一致。"""
    files = [_entry("file-1", "deep", "ABC-001.mp4", is_dir=False)]
    children = {"source": [_entry("mid", "source", "ABC-001", is_dir=True)]}
    breadcrumbs = {
        "deep": (
            "CD1",
            [
                DirBreadcrumb(file_id="0", name="根目录"),
                DirBreadcrumb(file_id="source", name="downloads"),
                DirBreadcrumb(file_id="mid", name="ABC-001"),
            ],
        )
    }
    client = _FakeSourceTreeClient(
        files=files, children_by_dir=children, breadcrumbs=breadcrumbs
    )

    _entries, rel_dirs = asyncio.run(
        collect_cloud115_source_files(client, "source", needs_rel_path=_is_video)
    )

    assert client.dir_info_calls == ["deep"]
    assert rel_dirs["deep"] == ("ABC-001", "CD1")


def test_source_scan_skips_layer_listing_when_all_files_sit_in_source():
    """文件全在源目录根下时，连层列举都不需要。"""
    files = [_entry("file-1", "source", "ABC-001.mp4", is_dir=False)]
    client = _FakeSourceTreeClient(files=files, children_by_dir={})

    _entries, rel_dirs = asyncio.run(
        collect_cloud115_source_files(client, "source", needs_rel_path=_is_video)
    )

    assert client.list_calls == []
    assert client.dir_info_calls == []
    assert rel_dirs["source"] == ()


def test_source_scan_ignores_parents_of_non_target_files():
    """字幕等非视频文件不触发父目录解析。"""
    files = [
        _entry("sub-1", "subs-only", "ABC-001.srt", is_dir=False),
        _entry("file-1", "source", "ABC-001.mp4", is_dir=False),
    ]
    client = _FakeSourceTreeClient(files=files, children_by_dir={})

    _entries, rel_dirs = asyncio.run(
        collect_cloud115_source_files(client, "source", needs_rel_path=_is_video)
    )

    assert client.list_calls == []
    assert client.dir_info_calls == []
    assert "subs-only" not in rel_dirs


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
    async def fake_client_for(_library, **_kwargs):
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
    async def fake_client_for(_library, **_kwargs):
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
    async def fake_client_for(_library, **_kwargs):
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
    # 目标目录 sha1 对账只属于 copy 模式：cleanup-source 已改为直接移动，不读目标目录。
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
        "transfer_mode": "copy",
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


# ---------------------------------------------------------------------------
# cleanup-source = 真正移动：不复制、不 re-list 目标、不删除已移动的视频源文件
# ---------------------------------------------------------------------------


class _FakeCloudClient:
    """记录全部远端调用的假 client；rename 之后 file_info 立即可见。"""

    def __init__(self, *, fail_on: str | None = None):
        self.calls: list = []
        self.names: dict[str, str] = {}
        self._fail_on = fail_on

    def _maybe_fail(self, kind: str) -> None:
        if self._fail_on == kind:
            raise RuntimeError(f"{kind} boom")

    async def move_files(self, fids, *, pid):
        self.calls.append(("move_files", tuple(fids), pid))
        self._maybe_fail("move_files")

    async def copy_files(self, fids, *, pid):
        self.calls.append(("copy_files", tuple(fids), pid))

    async def rename_file(self, fid, name):
        self.calls.append(("rename_file", fid, name))
        self._maybe_fail("rename_file")
        self.names[fid] = name

    async def delete_files(self, fids):
        self.calls.append(("delete_files", tuple(fids)))

    async def list_dir(self, cid, *, offset, limit):
        self.calls.append(("list_dir", cid))
        return [], 0

    async def file_info(self, fid):
        self.calls.append(("file_info", fid))
        return SimpleNamespace(name=self.names.get(fid, ""))

    @property
    def kinds(self) -> list[str]:
        return [call[0] for call in self.calls]


class _FakeTargetDirResolver:
    def __init__(self):
        self.version_cids: list[str] = []

    async def resolve_jav_entity(self, movie_number):
        return f"entity-{movie_number}", True

    async def create_version_dir(self, *, entity_cid, now_ms):
        del entity_cid, now_ms
        cid = f"version-{len(self.version_cids)}"
        self.version_cids.append(cid)
        return cid


def _source_file(
    *,
    fid: str = "src-fid",
    name: str = "ABC-001-hd.mp4",
    sha1: str = "SHA-A",
    subtitle=None,
):
    return CloudSourceFile(
        fid=fid,
        pickcode=f"pc-{fid}",
        name=name,
        sha1=sha1,
        size=4096,
        play_long=60,
        censored=False,
        rel_dir_parts=(),
        parent_cid="source",
        movie_number="ABC-001",
        subtitle=subtitle,
    )


@contextmanager
def _noop_atomic():
    yield


def _run_move_group(
    monkeypatch,
    *,
    files,
    existing_by_sha1: dict | None = None,
    client: _FakeCloudClient | None = None,
):
    """跑一遍 cleanup-source 分支，返回 (client, resolver, stats, failure_items, 登记记录)。"""
    existing_by_sha1 = existing_by_sha1 or {}
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.Cloud115MediaRegistrar"
        ".find_library_media",
        lambda library, sha1, *, valid=None: existing_by_sha1.get(sha1),
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.get_database",
        lambda: SimpleNamespace(atomic=_noop_atomic),
    )

    service = Cloud115ImportService()
    service._probe_cloud115_media = AsyncMock(
        return_value=SimpleNamespace(
            video_info={"codec": "h264"}, resolution="1080p", duration_seconds=60
        )
    )
    service._import_subtitle = AsyncMock()

    registered: list[dict] = []

    def fake_register(**kwargs):
        registered.append(kwargs)
        # 与真实 _register_media 一致：命中已有有效记录时只更新定位，返回 is_new=False。
        is_new = existing_by_sha1.get(kwargs["cloud_file"].sha1) is None
        return (
            SimpleNamespace(
                id=len(registered),
                backend_locator={
                    "fid": kwargs["target_fid"],
                    "pickcode": kwargs["target_pickcode"],
                    "name": kwargs["encoded_name"],
                    "source_path": kwargs["cloud_file"].rel_path,
                },
                save=lambda: None,
            ),
            is_new,
        )

    service._register_media = fake_register

    client = client or _FakeCloudClient()
    resolver = _FakeTargetDirResolver()
    stats = {"imported": 0, "skipped": 0, "failed": 0}
    failure_items: list[dict] = []

    asyncio.run(
        service._import_group_by_move(
            client,
            library=object(),
            movie=SimpleNamespace(id=7, movie_number="ABC-001", title="t"),
            group=CloudImportGroup(movie_number="ABC-001", files=list(files)),
            target_dir_resolver=resolver,
            failure_items=failure_items,
            stats=stats,
            new_playable_movies={},
        )
    )
    return client, resolver, stats, failure_items, registered


def test_cleanup_source_moves_instead_of_copying(monkeypatch):
    source = _source_file()
    client, resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch, files=[source]
    )

    assert client.calls == [
        ("move_files", ("src-fid",), "version-0"),
        ("rename_file", "src-fid", "ABC-001.mp4"),
        ("file_info", "src-fid"),
    ]
    # 不复制、不 re-list 目标目录、不删除已移动的视频源文件。
    assert "copy_files" not in client.kinds
    assert "list_dir" not in client.kinds
    assert "delete_files" not in client.kinds
    assert resolver.version_cids == ["version-0"]
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert failure_items == []
    # 登记用源 fid/pickcode，因为 move 不改这两个值。
    assert registered[0]["target_fid"] == "src-fid"
    assert registered[0]["target_pickcode"] == "pc-src-fid"


def test_cleanup_source_registers_before_moving(monkeypatch):
    """登记必须先于搬运：移动失败时记录已在、可播，源也还在原处。"""
    order: list[str] = []

    class _OrderedClient(_FakeCloudClient):
        async def move_files(self, fids, *, pid):
            order.append("move")
            await super().move_files(fids, pid=pid)

    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.Cloud115MediaRegistrar"
        ".find_library_media",
        lambda library, sha1, *, valid=None: None,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115_import_service.get_database",
        lambda: SimpleNamespace(atomic=_noop_atomic),
    )
    service = Cloud115ImportService()
    service._probe_cloud115_media = AsyncMock(
        return_value=SimpleNamespace(
            video_info={"codec": "h264"}, resolution="1080p", duration_seconds=60
        )
    )
    service._import_subtitle = AsyncMock()

    def fake_register(**kwargs):
        order.append("register")
        return SimpleNamespace(id=1, backend_locator={}, save=lambda: None), True

    service._register_media = fake_register

    asyncio.run(
        service._import_group_by_move(
            _OrderedClient(),
            library=object(),
            movie=SimpleNamespace(id=7, movie_number="ABC-001", title="t"),
            group=CloudImportGroup(movie_number="ABC-001", files=[_source_file()]),
            target_dir_resolver=_FakeTargetDirResolver(),
            failure_items=[],
            stats={"imported": 0, "skipped": 0, "failed": 0},
            new_playable_movies={},
        )
    )

    assert order == ["register", "move"]


def test_already_registered_same_fid_is_moved_not_deleted(monkeypatch):
    """上轮登记成功但没搬走：locator.fid 与源 fid 相同，必须补搬运而不是删源。"""
    source = _source_file()
    existing = SimpleNamespace(
        id=11,
        video_info={"codec": "h264"},
        backend_locator={"fid": "src-fid", "pickcode": "pc-src-fid"},
    )
    client, _resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch, files=[source], existing_by_sha1={"SHA-A": existing}
    )

    assert ("move_files", ("src-fid",), "version-0") in client.calls
    assert "delete_files" not in client.kinds
    assert len(registered) == 1
    # 续做搬运的文件算导入成功：用户刚重试完，不该看到"跳过"。
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert failure_items == []


def test_library_duplicate_with_other_fid_only_deletes_source(monkeypatch):
    """库里已有独立副本（fid 不同）：源是多余的一份，删掉即可，不再搬运。"""
    source = _source_file()
    existing = SimpleNamespace(
        id=12,
        video_info={"codec": "h264"},
        backend_locator={"fid": "other-fid", "pickcode": "pc-other"},
    )
    client, resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch, files=[source], existing_by_sha1={"SHA-A": existing}
    )

    assert client.calls == [("delete_files", ("src-fid",))]
    assert "move_files" not in client.kinds
    assert resolver.version_cids == []
    assert registered == []
    assert stats["skipped"] == 1
    assert failure_items[0]["reason"] == "duplicate_fingerprint"


def test_move_failure_keeps_source_and_stays_retryable(monkeypatch):
    """移动失败：不删源、不移回，只回收空版本目录，失败项保持可重导。"""
    client = _FakeCloudClient(fail_on="move_files")
    _client, _resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch, files=[_source_file()], client=client
    )

    assert client.kinds == ["move_files", "delete_files"]
    # 唯一的 delete 是回收本轮新建的空版本目录，不是删源文件。
    assert client.calls[1] == ("delete_files", ("version-0",))
    assert len(registered) == 1  # 登记已完成，Media 仍可播
    assert stats == {"imported": 0, "skipped": 0, "failed": 1}
    assert failure_items[0]["reason"] == "cloud115_transfer_failed"
    assert failure_items[0]["kind"] == "file"


def test_rename_failure_after_move_is_warning_and_restores_locator_name(monkeypatch):
    """改名失败时文件已在库、源已消失：降级为告警项，并把 locator.name 回写成实际名。"""
    client = _FakeCloudClient(fail_on="rename_file")
    _client, _resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch, files=[_source_file()], client=client
    )

    assert "move_files" in client.kinds
    assert "delete_files" not in client.kinds
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert failure_items[0]["reason"] == "cloud115_rename_failed"
    # 重导会因为源已不存在而无从下手，所以不能留成可重导项。
    assert failure_items[0]["kind"] == "warning"


def test_import_group_dispatches_by_transfer_mode(monkeypatch):
    service = Cloud115ImportService()
    service._import_group_by_move = AsyncMock()
    service._import_group_by_copy = AsyncMock()
    common = {
        "library": object(),
        "movie": object(),
        "group": CloudImportGroup(movie_number="ABC-001"),
        "target_dir_resolver": object(),
        "failure_items": [],
        "stats": {"imported": 0, "skipped": 0, "failed": 0},
        "new_playable_movies": {},
    }

    asyncio.run(service._import_group(object(), transfer_mode="cleanup-source", **common))
    service._import_group_by_move.assert_awaited_once()
    service._import_group_by_copy.assert_not_awaited()

    asyncio.run(service._import_group(object(), transfer_mode="copy", **common))
    service._import_group_by_copy.assert_awaited_once()
    service._import_group_by_move.assert_awaited_once()


# ---------------------------------------------------------------------------
# videos 导入：来源只遍历一次、videos/ 只解析一次、新实体目录直接建
# ---------------------------------------------------------------------------


def test_video_source_collection_lists_each_directory_once(monkeypatch):
    source_meta = DirMeta(
        cid="source",
        name="batch",
        pickcode="",
        parent_id="parent-cid",
        file_count=0,
        folder_count=0,
        play_long_seconds=0,
        mtime=0,
        ctime=0,
        paths=(DirBreadcrumb(file_id="parent-cid", name="parent"),),
    )
    monkeypatch.setattr(
        "src.service.videos.cloud115_video_import_service"
        ".assert_cid_outside_library_root",
        AsyncMock(return_value=source_meta),
    )

    client = _FakeSourceTreeClient(
        files=[
            _entry("f1", "source", "a.mp4", is_dir=False),
            _entry("f2", "sub", "b.mp4", is_dir=False),
        ],
        children_by_dir={
            "source": [
                _entry("sub", "source", "CD1", is_dir=True),
                # 不含视频的残留目录：不该被解析。
                _entry("stale", "source", "old-task", is_dir=True),
            ]
        },
    )
    display, sources = asyncio.run(
        Cloud115VideoImportService()._collect_sources(
            client,
            source_cid="source",
            source_fid=None,
            root_cid="root",
            only_files=None,
        )
    )

    # 一次整树递归 + 一次层列举即可；残留目录不产生 dir_info，且安全校验的元信息被复用。
    assert client.recursive_calls == ["source"]
    assert client.list_calls == [("source", 0, 1150)]
    assert client.dir_info_calls == []
    assert sorted(item.rel_path for item in sources) == ["CD1/b.mp4", "a.mp4"]
    assert display == "parent/batch"


def test_new_video_entity_dirs_are_created_without_listing():
    class FakeClient:
        def __init__(self):
            self.list_calls = []
            self.mkdir_calls = []

        async def list_dir(self, cid, *, offset, limit):
            del offset, limit
            self.list_calls.append(cid)
            entries = [_entry("videos-cid", "root", "videos", is_dir=True)]
            return entries, len(entries)

        async def mkdir(self, parent_cid, name):
            self.mkdir_calls.append((parent_cid, name))
            return f"created-{parent_cid}-{name}"

    client = FakeClient()
    resolver = Cloud115TargetDirResolver(client, root_cid="root")

    assert asyncio.run(
        resolver.create_new_videos_entity_dirs(video_id=31, now_ms=1700000000000)
    ) == ("created-videos-cid-31", "created-created-videos-cid-31-1700000000000")
    assert asyncio.run(
        resolver.create_new_videos_entity_dirs(video_id=32, now_ms=1700000000001)
    ) == ("created-videos-cid-32", "created-created-videos-cid-32-1700000000001")

    # videos/ 段目录整个作业只解析一次；每个新 video_id 只有两次 mkdir，零次列目录。
    assert client.list_calls == ["root"]
    assert client.mkdir_calls == [
        ("videos-cid", "31"),
        ("created-videos-cid-31", "1700000000000"),
        ("videos-cid", "32"),
        ("created-videos-cid-32", "1700000000001"),
    ]


def _run_video_cleanup_source(monkeypatch, *, existing, source_fid="src-fid"):
    """跑一遍 videos 的 cleanup-source 分流，返回被 mock 的 service。"""
    source = CloudSourceFile(
        fid=source_fid,
        pickcode="pc-src",
        name="a.mp4",
        sha1="SHA-A",
        size=2048,
        play_long=30,
        censored=False,
        rel_dir_parts=(),
        parent_cid="source",
    )

    @asynccontextmanager
    async def fake_client_for(_library, **_kwargs):
        yield SimpleNamespace()

    module = "src.service.videos.cloud115_video_import_service"
    monkeypatch.setattr(f"{module}.cloud115_client_for", fake_client_for)
    monkeypatch.setattr(
        f"{module}.require_cloud115_library", lambda _library: {"root_cid": "root"}
    )
    monkeypatch.setattr(
        f"{module}.Cloud115MediaRegistrar.find_library_media",
        lambda library, sha1, *, valid=None: existing,
    )

    service = Cloud115VideoImportService()
    service._collect_sources = AsyncMock(return_value=("display", [source]))
    service._move_registered_source = AsyncMock()
    service._cleanup_source_only = AsyncMock()
    service._import_file = AsyncMock()

    asyncio.run(
        service._run(
            library=object(),
            source_cid="source",
            source_fid=None,
            transfer_mode="cleanup-source",
            collection_id=None,
            only_files=None,
            failure_items=[],
            stats={"imported": 0, "skipped": 0, "failed": 0},
            progress_callback=None,
            job=SimpleNamespace(source_path="", save=lambda: None),
        )
    )
    return service


def test_video_registered_same_fid_is_moved_not_deleted(monkeypatch):
    """videos 侧同样的安全底线：locator.fid 就是这个源时补搬运，绝不能当重复副本删掉。"""
    service = _run_video_cleanup_source(
        monkeypatch,
        existing=SimpleNamespace(
            id=5, video_item_id=9, backend_locator={"fid": "src-fid"}
        ),
    )

    service._move_registered_source.assert_awaited_once()
    service._cleanup_source_only.assert_not_awaited()
    service._import_file.assert_not_awaited()


def test_video_library_duplicate_with_other_fid_deletes_source(monkeypatch):
    service = _run_video_cleanup_source(
        monkeypatch,
        existing=SimpleNamespace(
            id=6, video_item_id=9, backend_locator={"fid": "other-fid"}
        ),
    )

    service._cleanup_source_only.assert_awaited_once()
    service._move_registered_source.assert_not_awaited()
    service._import_file.assert_not_awaited()


def test_video_new_content_goes_through_import_file(monkeypatch):
    service = _run_video_cleanup_source(monkeypatch, existing=None)

    service._import_file.assert_awaited_once()
    service._move_registered_source.assert_not_awaited()
    service._cleanup_source_only.assert_not_awaited()


# ---------------------------------------------------------------------------
# 自动离线导入完成后清理来源任务目录
# ---------------------------------------------------------------------------


class _FakeDeleteClient:
    def __init__(self, *, fail: bool = False):
        self.deleted: list[list[str]] = []
        self._fail = fail

    async def delete_files(self, fids, *, pid=None):
        del pid
        if self._fail:
            raise RuntimeError("boom")
        self.deleted.append(list(fids))


def _run_source_cleanup(
    *,
    managed: bool,
    failed: int = 0,
    source_cid: str = "task-dir",
    download_root_cid: str | None = "downloads-root",
    client: _FakeDeleteClient | None = None,
):
    client = client or _FakeDeleteClient()
    failure_items: list[dict] = []
    stats = {"imported": 1, "skipped": 0, "failed": failed}
    config = {}
    if download_root_cid is not None:
        config["download_root_cid"] = download_root_cid
    asyncio.run(
        Cloud115ImportService._cleanup_managed_source_dir(
            client,
            config=config,
            source_cid=source_cid,
            managed_download_source=managed,
            failure_items=failure_items,
            stats=stats,
        )
    )
    return client, failure_items, stats


def test_managed_source_dir_is_deleted_after_clean_import():
    client, failure_items, _stats = _run_source_cleanup(managed=True)
    assert client.deleted == [["task-dir"]]
    assert failure_items == []


def test_manual_import_never_deletes_its_source_dir():
    """手动选的目录不属于软件自管缓冲区，一律不动。"""
    client, _failure_items, _stats = _run_source_cleanup(managed=False)
    assert client.deleted == []


def test_source_dir_is_kept_when_any_file_failed():
    """有失败项说明还需重导（如番号识别不出），源必须留着。"""
    client, _failure_items, _stats = _run_source_cleanup(managed=True, failed=1)
    assert client.deleted == []


def test_download_root_itself_is_never_deleted():
    client, _failure_items, _stats = _run_source_cleanup(
        managed=True, source_cid="downloads-root"
    )
    assert client.deleted == []


def test_missing_download_root_config_blocks_deletion():
    client, _failure_items, _stats = _run_source_cleanup(
        managed=True, download_root_cid=None
    )
    assert client.deleted == []


def test_source_dir_cleanup_failure_is_warning_only():
    """文件已入库，清理失败不该把作业翻成失败。"""
    _client, failure_items, stats = _run_source_cleanup(
        managed=True, client=_FakeDeleteClient(fail=True)
    )
    assert stats["failed"] == 0
    assert len(failure_items) == 1
    assert failure_items[0]["kind"] == "warning"
    assert failure_items[0]["reason"] == "source_delete_failed"
