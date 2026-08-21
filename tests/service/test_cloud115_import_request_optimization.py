import asyncio
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

from src.api.exception.errors import ApiError
from src.lib.cloud115 import (
    Cloud115Error,
    Cloud115NotFoundError,
    DirBreadcrumb,
    DirEntry,
    DirMeta,
)
from src.lib.cloud115.transport import Cloud115Transport
from src.schema.transfers.media_import import ImportRequest
from src.service.transfers.cloud115.importer.common import (
    Cloud115TargetDirCache,
    Cloud115TargetDirResolver,
    collect_cloud115_source_files,
)
from src.service.transfers.cloud115.importer.media_registrar import (
    Cloud115MediaRegistrar,
)
from src.service.transfers.cloud115.importer.scanner import scan_cloud115_source
from src.service.transfers.cloud115.importer.service import Cloud115ImportService
from src.service.transfers.cloud115.importer.strategies.common import register_media
from src.service.transfers.cloud115.importer.strategies.move import import_group_by_move
from src.service.transfers.cloud115.importer.types import (
    CloudImportGroup,
    CloudSourceFile,
    CloudSubtitleFile,
)
from src.service.transfers.cloud115.offline.sync_service import (
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


def test_deleted_cloud115_directory_error_is_not_found():
    error = Cloud115Transport._map_errno(
        {"state": False, "errNo": 70005, "error": "文件不存在或已删除"},
        endpoint="https://webapi.115.com/category/get",
    )

    assert isinstance(error, Cloud115NotFoundError)


def test_cloud115_errno_1001_is_parameter_error_not_not_found():
    error = Cloud115Transport._map_errno(
        {"state": False, "errNo": 1001, "error": "参数错误"},
        endpoint="https://webapi.115.com/category/get",
    )

    assert type(error) is Cloud115Error


def test_cloud115_category_not_found_errno_is_endpoint_scoped():
    error = Cloud115Transport._map_errno(
        {"state": False, "errNo": 70005, "error": "文件不存在或已删除"},
        endpoint="https://webapi.115.com/files/get_info",
    )

    assert type(error) is Cloud115Error


def test_cloud115_files_get_info_not_found_errno_is_endpoint_scoped():
    error = Cloud115Transport._map_errno(
        {"state": False, "errNo": 20018, "error": "文件不存在或已删除"},
        endpoint="https://webapi.115.com/files/get_info",
    )

    assert isinstance(error, Cloud115NotFoundError)


def test_new_cloud115_requests_are_move_only():
    for media_kind in ("jav", "video"):
        request = ImportRequest(
            media_kind=media_kind,
            backend="cloud115",
            library_id=1,
            source_cid="source",
            transfer_mode="cleanup-source",
        )
        assert request.transfer_mode == "cleanup-source"


def test_download_task_files_validates_cloud115_source_directory(monkeypatch):
    import pytest as _pytest

    client = SimpleNamespace()

    @asynccontextmanager
    async def fake_client_for(_library):
        yield client

    collect_files = AsyncMock(
        side_effect=Cloud115NotFoundError("source directory missing")
    )
    monkeypatch.setattr("src.service.cloud115.cloud115_client_for", fake_client_for)
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.common.collect_cloud115_source_files",
        collect_files,
    )

    from src.service.transfers.downloads.task_service import DownloadTaskService

    task = SimpleNamespace(
        id=123,
        target_ref={"cid": "missing"},
        client=SimpleNamespace(media_library=object()),
    )

    with _pytest.raises(ApiError) as exc_info:
        DownloadTaskService._list_cloud115_task_files(task)

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "cloud115_download_task_source_unavailable"
    collect_files.assert_awaited_once()


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


def test_managed_empty_source_is_kept_after_one_metadata_request(monkeypatch):
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
    client = SimpleNamespace(
        dir_info=AsyncMock(return_value=source_meta),
        delete_files=AsyncMock(),
    )

    @asynccontextmanager
    async def fake_client_for(_library, **_kwargs):
        yield client

    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.cloud115_client_for",
        fake_client_for,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.require_cloud115_library",
        lambda _library: {
            "root_cid": "library-root",
            "download_root_cid": "download-root",
        },
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.scan_cloud115_source",
        AsyncMock(return_value=([], 0, 0)),
    )
    service = Cloud115ImportService()
    failure_items: list[dict] = []
    stats = {"imported": 0, "skipped": 0, "failed": 0}
    asyncio.run(
        service._run(
            library=object(),
            source_cid="source",
            failure_items=failure_items,
            stats=stats,
            new_playable_movies={},
            progress_callback=None,
            managed_download_source=True,
            target_dir_cache=Cloud115TargetDirCache(),
        )
    )

    client.dir_info.assert_awaited_once_with("source")
    client.delete_files.assert_not_awaited()
    assert stats == {"imported": 0, "skipped": 0, "failed": 1}
    assert failure_items == [
        {
            "path": "cloud115:source",
            "reason": "no_media_files_found",
            "detail": "115 下载任务目录中没有扫描到可导入的视频",
            "kind": "job",
        }
    ]


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
        "src.service.transfers.cloud115.importer.service.cloud115_client_for",
        fake_client_for,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.require_cloud115_library",
        lambda _library: {
            "root_cid": "library-root",
            "download_root_cid": "download-root",
        },
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.assert_cid_outside_library_root",
        safety_check,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.Movie.get_by_id",
        lambda movie_id: SimpleNamespace(
            id=movie_id,
            movie_number=f"TEST-{movie_id:03d}",
            title="",
        ),
    )

    delays = iter((11.0, 29.0))
    monkeypatch.setattr(
        "src.common.service_helpers.random.uniform",
        lambda minimum, maximum: next(delays),
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.asyncio.sleep",
        sleep_mock,
    )

    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.scan_cloud115_source",
        AsyncMock(return_value=(groups, 0, 0)),
    )
    service = Cloud115ImportService(
        media_import_service=SimpleNamespace(
            metadata_import_batch=metadata_import_batch,
        )
    )
    service._import_group = AsyncMock()
    events = []
    shared_cache = Cloud115TargetDirCache() if managed_download_source else None

    asyncio.run(
        service._run(
            library=object(),
            source_cid="source",
            failure_items=[],
            stats={"imported": 0, "skipped": 0, "failed": 0},
            new_playable_movies={},
            progress_callback=events.append,
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


def test_auto_import_queue_rests_only_between_successful_jobs(monkeypatch):
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

    def trigger(task):
        task.import_status = "running"
        triggered.append(task.id)
        return SimpleNamespace(task_run_id=task.id)

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
        "_wait_for_import_task",
        staticmethod(
            lambda task_run_id: SimpleNamespace(
                id=task_run_id,
                state="completed",
                result_summary={"failed_count": 0},
            )
        ),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_reconcile_running_import",
        staticmethod(reconcile),
    )
    monkeypatch.setattr(
        "src.common.service_helpers.random.uniform",
        lambda minimum, maximum: 17.5,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.offline.sync_service.time.sleep",
        sleeps.append,
    )

    count = Cloud115OfflineSyncService._drain_pending_imports(client)

    assert count == 2
    assert triggered == [1, 2]
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

    def trigger(task):
        task.import_status = "running"
        triggered.append(task.id)
        return SimpleNamespace(task_run_id=task.id)

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
        "_wait_for_import_task",
        staticmethod(
            lambda task_run_id: SimpleNamespace(
                id=task_run_id,
                state="failed",
                result_summary={"failed_count": 1},
            )
        ),
    )
    monkeypatch.setattr(
        Cloud115OfflineSyncService,
        "_reconcile_running_import",
        staticmethod(reconcile),
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.offline.sync_service.time.sleep",
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
    files=None,
    group: CloudImportGroup | None = None,
    existing_by_sha1: dict | None = None,
    exact_by_sha1: dict | None = None,
    retained_duplicate_subtitle_fids: set[str] | None = None,
    client: _FakeCloudClient | None = None,
):
    """跑一遍 cleanup-source 分支，返回 (client, resolver, stats, failure_items, 登记记录)。"""
    existing_by_sha1 = existing_by_sha1 or {}
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.Cloud115MediaRegistrar"
        ".find_library_media",
        lambda library, sha1, *, valid=None: existing_by_sha1.get(sha1),
    )

    def find_movie_source_media(
        library,
        movie,
        sha1,
        *,
        locator,
        valid=None,
        for_update=False,
    ):
        del library, valid, for_update
        if exact_by_sha1 is not None:
            return exact_by_sha1.get(sha1)
        existing = existing_by_sha1.get(sha1)
        if existing is None:
            return None
        existing_locator = existing.backend_locator or {}
        if (
            existing.video_item_id is None
            and existing.movie_number == movie.movie_number
            and existing_locator.get("fid") == locator["fid"]
        ):
            return existing
        return None

    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.Cloud115MediaRegistrar"
        ".find_movie_source_media",
        find_movie_source_media,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.get_database",
        lambda: SimpleNamespace(atomic=_noop_atomic),
    )

    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.probe_cloud115_media",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    video_info={"codec": "h264"}, resolution="1080p", duration_seconds=60
                ),
                0,
            )
        ),
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.import_subtitle",
        AsyncMock(),
    )
    service = Cloud115ImportService()

    registered: list[dict] = []

    def fake_register(**kwargs):
        registered.append(kwargs)
        # move 只允许复用事务内精确命中的记录，不再按全域 SHA1 再查一次。
        validated_existing = kwargs["validated_existing"]
        is_new = validated_existing is None or not bool(validated_existing.valid)
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

    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.register_media",
        fake_register,
    )

    client = client or _FakeCloudClient()
    resolver = _FakeTargetDirResolver()
    stats = {"imported": 0, "skipped": 0, "failed": 0}
    failure_items: list[dict] = []
    move_group = group or CloudImportGroup(
        movie_number="ABC-001",
        files=list(files or ()),
        retained_duplicate_subtitle_fids=set(
            retained_duplicate_subtitle_fids or ()
        ),
    )

    asyncio.run(
        import_group_by_move(
            client,
            library=object(),
            movie=SimpleNamespace(id=7, movie_number="ABC-001", title="t"),
            group=move_group,
            target_dir_resolver=resolver,
            failure_items=failure_items,
            stats=stats,
            new_playable_movies={},
            probe_service=service._media_metadata_probe_service,
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
    assert registered[0]["validated_existing"] is None


def test_cleanup_source_registers_before_moving(monkeypatch):
    """登记必须先于搬运：移动失败时记录已在、可播，源也还在原处。"""
    order: list[str] = []

    class _OrderedClient(_FakeCloudClient):
        async def move_files(self, fids, *, pid):
            order.append("move")
            await super().move_files(fids, pid=pid)

    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.Cloud115MediaRegistrar"
        ".find_library_media",
        lambda library, sha1, *, valid=None: None,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.Cloud115MediaRegistrar"
        ".find_movie_source_media",
        lambda library, movie, sha1, *, locator, valid=None, for_update=False: None,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.get_database",
        lambda: SimpleNamespace(atomic=_noop_atomic),
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.probe_cloud115_media",
        AsyncMock(
            return_value=(
                SimpleNamespace(
                    video_info={"codec": "h264"}, resolution="1080p", duration_seconds=60
                ),
                0,
            )
        ),
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.import_subtitle",
        AsyncMock(),
    )
    service = Cloud115ImportService()

    def fake_register(**kwargs):
        order.append("register")
        return SimpleNamespace(id=1, backend_locator={}, save=lambda: None), True

    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.move.register_media",
        fake_register,
    )

    asyncio.run(
        import_group_by_move(
            _OrderedClient(),
            library=object(),
            movie=SimpleNamespace(id=7, movie_number="ABC-001", title="t"),
            group=CloudImportGroup(movie_number="ABC-001", files=[_source_file()]),
            target_dir_resolver=_FakeTargetDirResolver(),
            failure_items=[],
            stats={"imported": 0, "skipped": 0, "failed": 0},
            new_playable_movies={},
            probe_service=service._media_metadata_probe_service,
        )
    )

    assert order == ["register", "move"]


def test_already_registered_same_fid_is_moved_not_deleted(monkeypatch):
    """上轮登记成功但没搬走：locator.fid 与源 fid 相同，必须补搬运而不是删源。"""
    source = _source_file()
    existing = SimpleNamespace(
        id=11,
        movie_number="ABC-001",
        video_item_id=None,
        valid=True,
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


def test_library_duplicate_with_other_fid_keeps_source(monkeypatch):
    """库里已有独立副本（fid 不同）：只记重复，用户源文件保持不动。"""
    source = _source_file()
    existing = SimpleNamespace(
        id=12,
        movie_number="ABC-001",
        video_item_id=None,
        valid=True,
        video_info={"codec": "h264"},
        backend_locator={"fid": "other-fid", "pickcode": "pc-other"},
    )
    client, resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch, files=[source], existing_by_sha1={"SHA-A": existing}
    )

    assert client.calls == []
    assert "move_files" not in client.kinds
    assert resolver.version_cids == []
    assert registered == []
    assert stats["skipped"] == 1
    assert failure_items[0]["reason"] == "duplicate_fingerprint"


def test_video_item_media_with_same_fid_is_not_moved_into_jav(monkeypatch):
    """跨域同 FID 不是 JAV 续搬记录，必须按重复冲突保留在源目录。"""
    existing = SimpleNamespace(
        id=13,
        movie_number=None,
        video_item_id=9,
        valid=True,
        video_info={"codec": "h264"},
        backend_locator={"fid": "src-fid", "pickcode": "pc-src-fid"},
    )
    client, resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch,
        files=[_source_file()],
        existing_by_sha1={"SHA-A": existing},
    )

    assert client.calls == []
    assert resolver.version_cids == []
    assert registered == []
    assert stats == {"imported": 0, "skipped": 1, "failed": 0}
    assert failure_items[0]["reason"] == "duplicate_fingerprint"
    assert "独立的相同内容" in failure_items[0]["detail"]


def test_other_movie_media_with_same_fid_is_not_moved_into_current_jav(
    monkeypatch,
):
    existing = SimpleNamespace(
        id=15,
        movie_number="XYZ-999",
        video_item_id=None,
        valid=True,
        video_info={"codec": "h264"},
        backend_locator={"fid": "src-fid", "pickcode": "pc-src-fid"},
    )
    client, resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch,
        files=[_source_file()],
        existing_by_sha1={"SHA-A": existing},
    )

    assert client.calls == []
    assert resolver.version_cids == []
    assert registered == []
    assert stats == {"imported": 0, "skipped": 1, "failed": 0}
    assert failure_items[0]["reason"] == "duplicate_fingerprint"


def test_moved_video_deletes_unshared_source_subtitle(monkeypatch):
    subtitle = CloudSubtitleFile(
        fid="subtitle-fid",
        pickcode="subtitle-pickcode",
        name="ABC-001.srt",
    )
    client, _resolver, stats, failure_items, _registered = _run_move_group(
        monkeypatch,
        files=[_source_file(subtitle=subtitle)],
    )

    assert ("delete_files", ("subtitle-fid",)) in client.calls
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert failure_items == []


def test_shared_subtitle_is_retained_for_duplicate_source(monkeypatch):
    """成功搬走的视频不能删除仍被不同 FID 重复源引用的共享字幕。"""
    subtitle = CloudSubtitleFile(
        fid="shared-subtitle-fid",
        pickcode="shared-subtitle-pickcode",
        name="ABC-001.srt",
    )
    moved_source = _source_file(
        fid="move-fid",
        name="ABC-001-a.mp4",
        sha1="SHA-MOVE",
        subtitle=subtitle,
    )
    duplicate_source = _source_file(
        fid="duplicate-fid",
        name="ABC-001-b.mp4",
        sha1="SHA-DUPLICATE",
        subtitle=subtitle,
    )
    existing = SimpleNamespace(
        id=14,
        movie_number="ABC-001",
        video_item_id=None,
        valid=True,
        video_info={"codec": "h264"},
        backend_locator={"fid": "library-fid", "pickcode": "pc-library"},
    )

    client, _resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch,
        files=[moved_source, duplicate_source],
        existing_by_sha1={"SHA-DUPLICATE": existing},
    )

    assert ("move_files", ("move-fid",), "version-0") in client.calls
    assert ("delete_files", ("shared-subtitle-fid",)) not in client.calls
    assert [item["cloud_file"].fid for item in registered] == ["move-fid"]
    assert stats == {"imported": 1, "skipped": 1, "failed": 0}
    assert [item["reason"] for item in failure_items] == ["duplicate_fingerprint"]


def test_scanner_retains_shared_subtitle_reference_from_same_sha1_duplicate(
    monkeypatch,
):
    """scanner 丢弃同 SHA1 后续视频时，必须把其字幕引用显式传给 move。"""
    shared_sha1 = "A" * 40
    entries = [
        DirEntry(
            entry_id="video-first",
            parent_id="source",
            name="ABC-001-a.mp4",
            is_dir=False,
            size=4096,
            sha1=shared_sha1,
            pickcode="pc-first",
            mtime=0,
            ctime=0,
            is_video=True,
        ),
        DirEntry(
            entry_id="video-duplicate",
            parent_id="source",
            name="ABC-001-b.mp4",
            is_dir=False,
            size=4096,
            sha1=shared_sha1,
            pickcode="pc-duplicate",
            mtime=0,
            ctime=0,
            is_video=True,
        ),
        DirEntry(
            entry_id="shared-subtitle",
            parent_id="source",
            name="ABC-001.srt",
            is_dir=False,
            size=128,
            sha1="B" * 40,
            pickcode="pc-subtitle",
            mtime=0,
            ctime=0,
            is_video=False,
        ),
    ]
    scan_client = _FakeSourceTreeClient(files=entries, children_by_dir={})
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.scanner.settings",
        SimpleNamespace(media=SimpleNamespace(allowed_min_video_file_size=0)),
    )
    failure_items: list[dict] = []
    groups, skipped, failed = asyncio.run(
        scan_cloud115_source(
            scan_client,
            library=object(),
            source_cid="source",
            source_name="downloads",
            failure_items=failure_items,
        )
    )

    assert len(groups) == 1
    group = groups[0]
    assert [source.fid for source in group.files] == ["video-first"]
    assert group.retained_duplicate_subtitle_fids == {"shared-subtitle"}
    assert skipped == 1
    assert failed == 0
    assert failure_items[0]["reason"] == "duplicate_fingerprint"

    move_client, _resolver, stats, move_failures, _registered = _run_move_group(
        monkeypatch,
        group=group,
    )
    assert ("move_files", ("video-first",), "version-0") in move_client.calls
    assert ("delete_files", ("shared-subtitle",)) not in move_client.calls
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert move_failures == []


def test_exact_current_movie_media_wins_over_other_fingerprint_matches(monkeypatch):
    """同指纹多 Media 时，只复用当前 Movie + 完整源 locator 的精确记录。"""
    current_media = SimpleNamespace(
        id=21,
        movie_number="ABC-001",
        video_item_id=None,
        valid=True,
        video_info={"codec": "h264"},
        backend_locator={"fid": "src-fid", "pickcode": "pc-src-fid"},
    )
    other_media = SimpleNamespace(
        id=22,
        movie_number=None,
        video_item_id=9,
        valid=True,
        video_info={"codec": "h265"},
        backend_locator={"fid": "video-fid", "pickcode": "pc-video-fid"},
    )

    client, _resolver, stats, failure_items, registered = _run_move_group(
        monkeypatch,
        files=[_source_file()],
        existing_by_sha1={"SHA-A": other_media},
        exact_by_sha1={"SHA-A": current_media},
    )

    assert ("move_files", ("src-fid",), "version-0") in client.calls
    assert registered[0]["validated_existing"] is current_media
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert failure_items == []


def test_validated_new_move_registration_ignores_concurrent_cross_domain_match(
    monkeypatch,
):
    """扫描后并发插入的跨域同指纹 Media 不能被 move 登记阶段复用。"""
    concurrent_video_media = SimpleNamespace(
        id=31,
        valid=True,
        video_item_id=9,
        backend_locator={"fid": "src-fid"},
        save=Mock(),
    )
    global_lookup = Mock(return_value=concurrent_video_media)
    created_media = SimpleNamespace(id=32)
    create_media = Mock(return_value=created_media)
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.common."
        "Cloud115MediaRegistrar.find_library_media",
        global_lookup,
    )
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.strategies.common."
        "Cloud115MediaRegistrar.create_cloud115_media",
        create_media,
    )
    # 缩略图待处理状态改由 Media + MediaThumbnail 实时查询，不再维护写侧重置钩子。
    assert not hasattr(Cloud115MediaRegistrar, "reset_thumbnail_state")
    movie = SimpleNamespace(movie_number="ABC-001")

    media, is_new = register_media(
        library=object(),
        movie=movie,
        cloud_file=_source_file(),
        target_fid="src-fid",
        target_pickcode="pc-src-fid",
        encoded_name="ABC-001.mp4",
        metadata=SimpleNamespace(
            video_info={"codec": "h264"},
            resolution="1080p",
            duration_seconds=60,
        ),
        validated_existing=None,
    )

    assert media is created_media
    assert is_new is True
    global_lookup.assert_not_called()
    concurrent_video_media.save.assert_not_called()
    assert create_media.call_args.kwargs["movie"] is movie


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
    _client, _resolver, stats, failure_items, _registered = _run_move_group(
        monkeypatch, files=[_source_file()], client=client
    )

    assert "move_files" in client.kinds
    assert "delete_files" not in client.kinds
    assert stats == {"imported": 1, "skipped": 0, "failed": 0}
    assert failure_items[0]["reason"] == "cloud115_rename_failed"
    # 重导会因为源已不存在而无从下手，所以不能留成可重导项。
    assert failure_items[0]["kind"] == "warning"


def test_import_group_always_uses_move(monkeypatch):
    move_mock = AsyncMock()
    monkeypatch.setattr(
        "src.service.transfers.cloud115.importer.service.import_group_by_move",
        move_mock,
    )
    service = Cloud115ImportService()
    common = {
        "library": object(),
        "movie": object(),
        "group": CloudImportGroup(movie_number="ABC-001"),
        "target_dir_resolver": object(),
        "failure_items": [],
        "stats": {"imported": 0, "skipped": 0, "failed": 0},
        "new_playable_movies": {},
    }

    asyncio.run(service._import_group(object(), **common))
    move_mock.assert_awaited_once()


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
    service._import_file = AsyncMock()
    failure_items: list[dict] = []
    stats = {"imported": 0, "skipped": 0, "failed": 0}

    asyncio.run(
        service._run(
            library=object(),
            source_cid="source",
            source_fid=None,
            collection_id=None,
            failure_items=failure_items,
            stats=stats,
            progress_callback=None,
        )
    )
    return service, failure_items, stats


def test_cloud115_video_executor_returns_actual_counts(test_db, monkeypatch):
    """公共执行器的稳定结果来自真实扫描循环统计，不依赖固定结果 mock。"""
    from src.model import MediaLibrary

    library = MediaLibrary.create(
        name="cloud-video-counts",
        backend="cloud115",
        backend_account_key="cloud115:video-counts",
        backend_config={"cookies": "UID=1_A1_1700000000", "root_cid": "root"},
    )
    sources = [
        _source_file(fid="new", sha1="SHA-NEW"),
        _source_file(fid="duplicate", sha1="SHA-NEW"),
        _source_file(fid="missing-sha1", sha1=""),
    ]

    @asynccontextmanager
    async def fake_client_for(_library, **_kwargs):
        yield SimpleNamespace()

    module = "src.service.videos.cloud115_video_import_service"
    monkeypatch.setattr(f"{module}.cloud115_client_for", fake_client_for)
    monkeypatch.setattr(
        f"{module}.Cloud115MediaRegistrar.find_library_media",
        lambda *_args, **_kwargs: None,
    )
    service = Cloud115VideoImportService()
    service._collect_sources = AsyncMock(return_value=("source", sources))

    async def import_file(*_args, **kwargs):
        kwargs["stats"]["imported"] += 1

    service._import_file = AsyncMock(side_effect=import_file)

    result = service.import_from_cloud115(library.id, source_cid="source")

    assert result.model_dump(
        include={"imported_count", "skipped_count", "failed_count"}
    ) == {"imported_count": 1, "skipped_count": 1, "failed_count": 1}
    service._import_file.assert_awaited_once()


def test_video_registered_same_fid_is_moved_not_deleted(monkeypatch):
    """videos 侧同样的安全底线：locator.fid 就是这个源时补搬运，绝不能当重复副本删掉。"""
    service, failure_items, _stats = _run_video_cleanup_source(
        monkeypatch,
        existing=SimpleNamespace(
            id=5, video_item_id=9, backend_locator={"fid": "src-fid"}
        ),
    )

    service._move_registered_source.assert_awaited_once()
    service._import_file.assert_not_awaited()
    assert failure_items == []


def test_video_library_duplicate_with_other_fid_keeps_source(monkeypatch):
    service, failure_items, stats = _run_video_cleanup_source(
        monkeypatch,
        existing=SimpleNamespace(
            id=6, video_item_id=9, backend_locator={"fid": "other-fid"}
        ),
    )

    service._move_registered_source.assert_not_awaited()
    service._import_file.assert_not_awaited()
    assert stats == {"imported": 0, "skipped": 1, "failed": 0}
    assert failure_items[0]["reason"] == "duplicate_fingerprint"


def test_video_new_content_goes_through_import_file(monkeypatch):
    service, failure_items, _stats = _run_video_cleanup_source(
        monkeypatch, existing=None
    )

    service._import_file.assert_awaited_once()
    service._move_registered_source.assert_not_awaited()
    assert failure_items == []


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
    imported: int = 1,
    skipped: int = 0,
    failed: int = 0,
    source_cid: str = "task-dir",
    download_root_cid: str | None = "downloads-root",
    client: _FakeDeleteClient | None = None,
    failure_items: list[dict] | None = None,
):
    client = client or _FakeDeleteClient()
    failure_items = list(failure_items or [])
    stats = {"imported": imported, "skipped": skipped, "failed": failed}
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


def test_source_dir_is_kept_when_no_media_was_imported():
    """零产出但有跳过候选时保留源目录，供用户查看和重导。"""
    client, _failure_items, _stats = _run_source_cleanup(
        managed=True, imported=0, skipped=6
    )
    assert client.deleted == []


def test_source_dir_is_kept_when_mixed_import_contains_preserved_duplicate():
    """同目录另有成功项时，也不能靠整目录清理绕过重复源保留语义。"""
    client, failure_items, _stats = _run_source_cleanup(
        managed=True,
        imported=1,
        skipped=1,
        failure_items=[
            {
                "path": "duplicate.mp4",
                "reason": "duplicate_fingerprint",
                "detail": "库中已存在相同内容",
                "kind": "skipped",
            }
        ],
    )
    assert client.deleted == []
    assert failure_items[0]["reason"] == "duplicate_fingerprint"


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


# ---------------------------------------------------------------------------
# 离线提交：任务目录直接 mkdir，只在 errno=20004 时回退定位
# ---------------------------------------------------------------------------


def test_offline_task_dir_is_created_without_scanning():
    """info_hash 全局唯一，旧实现那次全量翻页必然扫不中——现在直接 mkdir。"""
    from src.service.transfers.cloud115.offline.service import _create_task_dir

    class FakeClient:
        def __init__(self):
            self.list_calls = []
            self.mkdir_calls = []

        async def list_dir(self, cid, *, offset, limit):
            self.list_calls.append(cid)
            return [], 0

        async def mkdir(self, pid, name):
            self.mkdir_calls.append((pid, name))
            return f"cid-{name}"

    client = FakeClient()
    cid = asyncio.run(
        _create_task_dir(client, download_root_cid="root", info_hash="abc123")
    )

    assert cid == "cid-abc123"
    assert client.mkdir_calls == [("root", "abc123")]
    assert client.list_calls == []          # 不再翻下载根目录


def test_offline_task_dir_falls_back_to_lookup_on_duplicate_name():
    """上一轮中断留下孤儿目录时，115 回 errno=20004，此时才分页定位复用。"""
    from src.lib.cloud115 import Cloud115DuplicateNameError
    from src.service.transfers.cloud115.offline.service import _create_task_dir

    class FakeClient:
        def __init__(self):
            self.list_calls = []
            self.mkdir_calls = []

        async def list_dir(self, cid, *, offset, limit):
            self.list_calls.append(cid)
            entries = [_entry("existing-cid", "root", "abc123", is_dir=True)]
            return entries, len(entries)

        async def mkdir(self, pid, name):
            self.mkdir_calls.append((pid, name))
            raise Cloud115DuplicateNameError("该目录名称已存在。 (errno=20004)", errno=20004)

    client = FakeClient()
    cid = asyncio.run(
        _create_task_dir(client, download_root_cid="root", info_hash="abc123")
    )

    assert cid == "existing-cid"
    assert client.list_calls == ["root"]


def test_offline_task_dir_does_not_swallow_risk_control():
    """webapi 域的裸 HTTP 400 是 WAF 签名（transport 映射为风控），绝不能当成重名去兜底重试。"""
    import pytest as _pytest

    from src.lib.cloud115 import Cloud115RiskControlError
    from src.service.transfers.cloud115.offline.service import _create_task_dir

    class FakeClient:
        def __init__(self):
            self.list_calls = []

        async def list_dir(self, cid, *, offset, limit):
            self.list_calls.append(cid)
            return [], 0

        async def mkdir(self, pid, name):
            raise Cloud115RiskControlError(
                "http 400 (risk control / WAF) on POST https://webapi.115.com/files/add",
                method="POST",
                url="https://webapi.115.com/files/add",
            )

    client = FakeClient()
    with _pytest.raises(Cloud115RiskControlError):
        asyncio.run(_create_task_dir(client, download_root_cid="root", info_hash="x"))
    assert client.list_calls == []


# ---------------------------------------------------------------------------
# 订阅自动下载：cloud115 提交之间的休息
# ---------------------------------------------------------------------------


def test_subscription_submits_rest_only_between_cloud115_submissions(monkeypatch):
    """qB 提交不碰 115，不该白等；cloud115 提交之间才需要排队。"""
    from src.service.transfers.downloads.auto_subscribed import (
        auto_download_service as mod,
    )

    svc = mod.SubscribedMovieAutoDownloadService.__new__(
        mod.SubscribedMovieAutoDownloadService
    )
    cloud_ids = {7}
    assert svc._is_cloud115_task(7, cloud_ids) is True     # cloud115 入口
    assert svc._is_cloud115_task(9, cloud_ids) is False    # qB 入口
    assert svc._is_cloud115_task(None, cloud_ids) is False


def test_subscription_rest_window_matches_import_side():
    """与导入侧番号间休息保持同一量级，避免两套不一致的节奏常量。"""
    from src.service.transfers.cloud115.importer.service import (
        MANUAL_GROUP_REST_MAX_SECONDS,
        MANUAL_GROUP_REST_MIN_SECONDS,
    )
    from src.service.transfers.downloads.auto_subscribed.auto_download_service import (
        SUBMIT_REST_MAX_SECONDS,
        SUBMIT_REST_MIN_SECONDS,
    )

    assert (SUBMIT_REST_MIN_SECONDS, SUBMIT_REST_MAX_SECONDS) == (
        MANUAL_GROUP_REST_MIN_SECONDS,
        MANUAL_GROUP_REST_MAX_SECONDS,
    )


# ---------------------------------------------------------------------------
# errno=20004 的接入层处理：竞态自愈 + 错误映射
# ---------------------------------------------------------------------------


def test_find_or_create_subdir_reuses_concurrently_created_dir():
    """扫描→mkdir 之间被并发抢先建好：115 拒绝重名，重扫取对方的 cid 收敛。"""
    from src.lib.cloud115 import Cloud115DuplicateNameError
    from src.service.cloud115 import find_or_create_subdir

    class FakeClient:
        def __init__(self):
            self.scans = 0
            self.exists_after_first_scan = False

        async def list_dir(self, cid, *, offset, limit):
            self.scans += 1
            if self.exists_after_first_scan:
                entries = [_entry("winner-cid", cid, "jav", is_dir=True)]
                return entries, len(entries)
            # 第一次扫描时还不存在
            self.exists_after_first_scan = True
            return [], 0

        async def mkdir(self, pid, name):
            raise Cloud115DuplicateNameError("该目录名称已存在。 (errno=20004)", errno=20004)

    client = FakeClient()
    cid = asyncio.run(find_or_create_subdir(client, parent_cid="root", name="jav"))

    assert cid == "winner-cid"
    assert client.scans == 2      # 首扫未命中 + 撞名后重扫


def test_find_or_create_subdir_raises_when_duplicate_but_not_findable():
    """115 说重名、重扫又找不到：状态自相矛盾，不静默吞。"""
    import pytest as _pytest

    from src.lib.cloud115 import Cloud115DuplicateNameError
    from src.service.cloud115 import find_or_create_subdir

    class FakeClient:
        async def list_dir(self, cid, *, offset, limit):
            return [], 0

        async def mkdir(self, pid, name):
            raise Cloud115DuplicateNameError("该目录名称已存在。 (errno=20004)", errno=20004)

    with _pytest.raises(Cloud115DuplicateNameError):
        asyncio.run(find_or_create_subdir(FakeClient(), parent_cid="root", name="jav"))


def test_entity_dir_race_marks_not_created_so_reconciliation_still_runs():
    """并发建好的番号目录可能已有文件，created 必须回落为 False，否则会跳过 SHA1 对账。"""
    from src.lib.cloud115 import Cloud115DuplicateNameError

    class FakeClient:
        def __init__(self):
            self.mkdir_calls = []
            self.list_calls = []

        async def list_dir(self, cid, *, offset, limit):
            self.list_calls.append(cid)
            if cid == "root":
                entries = [_entry("jav-cid", "root", "jav", is_dir=True)]
            elif cid == "jav-cid" and len(self.list_calls) > 2:
                # 撞名后的重扫：另一个作业建好的番号目录已可见
                entries = [_entry("rival-cid", "jav-cid", "ABC-001", is_dir=True)]
            else:
                entries = []
            return entries, len(entries)

        async def mkdir(self, pid, name):
            self.mkdir_calls.append((pid, name))
            raise Cloud115DuplicateNameError("该目录名称已存在。 (errno=20004)", errno=20004)

    client = FakeClient()
    resolver = Cloud115TargetDirResolver(client, root_cid="root")
    cid, created = asyncio.run(resolver.resolve_jav_entity("ABC-001"))

    assert cid == "rival-cid"
    assert created is False        # ← 关键：不能当成"本轮新建"跳过对账


def test_duplicate_name_maps_to_conflict_not_generic_upstream_error():
    """20004 不该掉进兜底的 502 上游错误。"""
    from src.lib.cloud115 import Cloud115DuplicateNameError, Cloud115Error
    from src.service.cloud115 import map_cloud115_error

    err = map_cloud115_error(
        Cloud115DuplicateNameError("该目录名称已存在。 (errno=20004)", errno=20004)
    )
    assert err.status_code == 409
    assert err.code == "cloud115_duplicate_name"
    # 未识别的错误仍走兜底
    assert map_cloud115_error(Cloud115Error("boom")).status_code == 502
