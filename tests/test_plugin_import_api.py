"""插件导入门面与视频合集契约：浏览 provider 存储、发起导入、按名称复用合集。"""

from __future__ import annotations

from datetime import datetime

import pytest
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.model import (
    BackgroundTaskRun,
    MediaLibrary,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
)
from src.plugins import (
    PluginBrowseEntry,
    PluginBrowsePage,
    PluginCollection,
    PluginContext,
    PluginImportBatch,
    PluginImportStatus,
    PluginLibrary,
)
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    BrowseEntry,
    BrowsePage,
    ProviderOperationError,
)
from src.service.videos.video_collection_service import VideoCollectionService


def _library(name: str = "插件导入库") -> MediaLibrary:
    return MediaLibrary.create(
        name=name,
        provider_key="local",
        provider_config={"web_cookie": "hidden"},
    )


class _FakeStorage:
    def __init__(self, page: BrowsePage | Exception):
        self.page = page
        self.calls: list[dict] = []

    def browse(self, *, parent_ref, cursor, limit):
        self.calls.append({"parent_ref": parent_ref, "cursor": cursor, "limit": limit})
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


def _patch_storage(monkeypatch, storage: _FakeStorage) -> None:
    monkeypatch.setattr(MEDIA_PROVIDER_REGISTRY, "storage_for", lambda _handle: storage)


def test_list_libraries_exposes_identity_without_provider_config(test_db, tmp_path):
    first = _library("导入库A")
    second = _library("导入库B")

    libraries = PluginContext("import_demo", {}, tmp_path).imports.list_libraries()

    assert libraries == (
        PluginLibrary(library_id=first.id, name="导入库A", provider_key="local"),
        PluginLibrary(library_id=second.id, name="导入库B", provider_key="local"),
    )
    assert not hasattr(libraries[0], "provider_config")


def test_browse_resolves_library_and_maps_provider_entries(
    test_db, tmp_path, monkeypatch
):
    _library("浏览库")
    storage = _FakeStorage(
        BrowsePage(
            entries=(
                BrowseEntry(
                    source_ref={"kind": "manual_source", "relative_path": "clip.mp4"},
                    name="clip.mp4",
                    entry_type="file",
                    size_bytes=2048,
                    modified_at=datetime(2026, 9, 1, 12, 0, 0),
                    is_video=True,
                ),
                BrowseEntry(
                    source_ref={"kind": "manual_source", "relative_path": "notes.txt"},
                    name="notes.txt",
                    entry_type="file",
                    size_bytes=16,
                    modified_at=None,
                    is_video=False,
                ),
                BrowseEntry(
                    source_ref={"kind": "manual_source", "relative_path": "待整理"},
                    name="待整理",
                    entry_type="directory",
                    size_bytes=None,
                    modified_at=None,
                    is_video=False,
                ),
            ),
            next_cursor="3",
        )
    )
    _patch_storage(monkeypatch, storage)

    page = PluginContext("import_demo", {}, tmp_path).imports.browse(
        library="浏览库", parent_ref=None, cursor=None, limit=10
    )

    assert isinstance(page, PluginBrowsePage)
    assert [entry.name for entry in page.entries] == ["clip.mp4", "notes.txt", "待整理"]
    assert page.entries[0] == PluginBrowseEntry(
        source_ref={"kind": "manual_source", "relative_path": "clip.mp4"},
        name="clip.mp4",
        entry_type="file",
        size_bytes=2048,
        modified_at=datetime(2026, 9, 1, 12, 0, 0),
        is_video=True,
    )
    assert page.entries[1].is_video is False
    assert page.entries[2].entry_type == "directory"
    assert page.next_cursor == "3"
    assert storage.calls == [{"parent_ref": None, "cursor": None, "limit": 10}]


def test_browse_rejects_missing_library_by_id_and_name(test_db, tmp_path):
    api = PluginContext("import_demo", {}, tmp_path).imports

    with pytest.raises(ApiError) as by_id:
        api.browse(library=9999)
    with pytest.raises(ApiError) as by_name:
        api.browse(library="不存在的库")

    assert by_id.value.status_code == 404
    assert by_name.value.status_code == 404
    assert by_id.value.code == "media_library_not_found"


def test_browse_maps_provider_errors(test_db, tmp_path, monkeypatch):
    _library("错误库")
    _patch_storage(
        monkeypatch,
        _FakeStorage(
            ProviderOperationError(
                "local",
                "browse",
                "authentication_failed",
                "认证失败",
                False,
            )
        ),
    )

    with pytest.raises(ApiError) as caught:
        PluginContext("import_demo", {}, tmp_path).imports.browse(library="错误库")

    assert caught.value.status_code == 401
    assert caught.value.code == "provider_authentication_failed"


def test_enqueue_video_creates_plugin_triggered_import_task(test_db, tmp_path):
    library = _library("入队库")
    context = PluginContext("import_demo", {}, tmp_path)
    collection = context.collections.ensure_video_collection("推特视频")

    batch = context.imports.enqueue(
        media_kind="video",
        library=library.id,
        source_ref={"kind": "manual_source", "relative_path": "clip.mp4"},
        collection_id=collection.collection_id,
    )

    assert isinstance(batch, PluginImportBatch)
    assert batch.task_key == "library_import"
    assert batch.state == "pending"
    task_run = BackgroundTaskRun.get_by_id(batch.task_run_id)
    assert task_run.trigger_type == "plugin"
    assert task_run.params["media_kind"] == "video"
    assert task_run.params["library_id"] == library.id
    assert task_run.params["source_ref"] == {
        "kind": "manual_source",
        "relative_path": "clip.mp4",
    }
    assert task_run.params["collection_id"] == collection.collection_id


def test_enqueue_rejects_jav_collection_and_conflicting_library(test_db, tmp_path):
    library = _library("冲突库")
    api = PluginContext("import_demo", {}, tmp_path).imports

    with pytest.raises(ValueError):
        api.enqueue(
            media_kind="jav",
            library=library.id,
            source_ref={"kind": "manual_source", "relative_path": "ABP-001.mp4"},
            collection_id=1,
        )

    accepted = api.enqueue(
        media_kind="video",
        library=library.id,
        source_ref={"kind": "manual_source", "relative_path": "clip.mp4"},
    )
    assert accepted.state == "pending"

    with pytest.raises(ApiError) as caught:
        api.enqueue(
            media_kind="video",
            library=library.id,
            source_ref={"kind": "manual_source", "relative_path": "other.mp4"},
        )
    assert caught.value.status_code == 409
    assert caught.value.code == "import_task_conflict"


def test_enqueue_requires_provider_source_ref(test_db, tmp_path):
    _library("空引用库")
    api = PluginContext("import_demo", {}, tmp_path).imports

    with pytest.raises(ValueError):
        api.enqueue(media_kind="video", library="空引用库", source_ref={})


def test_ensure_video_collection_reuses_same_name_without_overwriting(
    test_db, tmp_path
):
    context = PluginContext("import_demo", {}, tmp_path)
    user_created = VideoCollection.create(name="推上收藏", description="用户先建")

    reused = context.collections.ensure_video_collection("推上收藏", "插件描述")
    created = context.collections.ensure_video_collection("推特视频", "插件新建")

    assert isinstance(reused, PluginCollection)
    assert reused.collection_type == "video"
    assert reused.collection_id == user_created.id
    assert reused.key == ""
    # 同名合集原样复用：插件不覆盖用户设置的名字与描述。
    assert reused.description == "用户先建"

    assert created.collection_id != reused.collection_id
    assert created.name == "推特视频"
    assert created.description == "插件新建"
    assert created.member_count == 0

    video = VideoItem.create(title="片段")
    VideoCollectionItem.create(collection=user_created, video_item=video, position=0)
    assert context.collections.ensure_video_collection("推上收藏").member_count == 1

    with pytest.raises(ValueError):
        context.collections.ensure_video_collection("   ")


def test_get_or_create_by_name_recovers_from_concurrent_create(test_db, monkeypatch):
    existing = VideoCollection.create(name="并发合集")
    real_get_or_none = VideoCollection.get_or_none
    calls = {"count": 0}

    def flaky_get_or_none(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return real_get_or_none(*args, **kwargs)

    def failing_create(**kwargs):
        raise IntegrityError("duplicate key value violates unique constraint")

    monkeypatch.setattr(VideoCollection, "get_or_none", flaky_get_or_none)
    monkeypatch.setattr(VideoCollection, "create", failing_create)

    collection = VideoCollectionService.get_or_create_by_name("并发合集")

    assert collection.id == existing.id
    assert calls["count"] == 2


def test_imports_get_returns_own_task_status(test_db, tmp_path):
    library = _library("状态库")
    context = PluginContext("import_demo", {}, tmp_path)

    accepted = context.imports.enqueue(
        media_kind="video",
        library=library.id,
        source_ref={"kind": "manual_source", "relative_path": "clip.mp4"},
    )
    pending = context.imports.get(accepted.task_run_id)
    assert isinstance(pending, PluginImportStatus)
    assert pending.state == "pending"
    assert pending.imported_count == 0
    assert pending.skipped_count == 0
    assert pending.failed_count == 0
    assert pending.created_video_ids == ()
    assert pending.movie_ids == ()
    assert pending.error_message is None

    task_run = BackgroundTaskRun.get_by_id(accepted.task_run_id)
    task_run.state = "completed"
    task_run.result_summary = {
        "imported_count": 1,
        "skipped_count": 2,
        "failed_count": 0,
        "created_video_ids": [7, 8],
        "new_playable_movies": [
            {"id": 5, "movie_number": "ABP-001", "title": "影片"},
        ],
    }
    task_run.save(only=[BackgroundTaskRun.state, BackgroundTaskRun.result_summary])

    status = context.imports.get(accepted.task_run_id)
    assert status.state == "completed"
    assert status.imported_count == 1
    assert status.skipped_count == 2
    assert status.failed_count == 0
    assert status.created_video_ids == (7, 8)
    assert status.movie_ids == (5,)


def test_imports_get_scopes_to_owning_plugin(test_db, tmp_path):
    library = _library("归属库")
    owner = PluginContext("import_demo", {}, tmp_path)
    other = PluginContext("other_demo", {}, tmp_path)

    accepted = owner.imports.enqueue(
        media_kind="video",
        library=library.id,
        source_ref={"kind": "manual_source", "relative_path": "clip.mp4"},
    )

    assert other.imports.get(accepted.task_run_id) is None
    assert owner.imports.get(999999) is None
