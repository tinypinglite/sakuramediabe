from __future__ import annotations

import itertools
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from src.api.exception.errors import ApiError
from src.lib.cloud115 import Cloud115NotFoundError
from src.lib.cloud115.types import DirectUrl, DirBreadcrumb, DirEntry, DirMeta, FileMeta
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
    ResourceTaskState,
    SystemEvent,
    SystemNotification,
    VideoCollection,
    VideoCollectionItem,
    VideoImportJob,
    VideoItem,
)
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeResult,
)
from src.service.videos import cloud115_video_import_service as service_module
from src.service.videos import cloud115_video_import_job_service as job_module
from src.service.transfers import cloud115_import_job_service as jav_job_module
from src.service.transfers.cloud115_import_job_service import Cloud115ImportJobService
from src.service.transfers.import_runner import DownloadImportRunner
from src.service.videos.cloud115_video_import_job_service import Cloud115VideoImportJobService
from src.service.videos.cloud115_video_import_service import Cloud115VideoImportService


@dataclass
class _File:
    fid: str
    parent: str
    name: str
    sha1: str
    size: int = 1024
    pickcode: str = ""
    play_long: int | None = 120
    ic: int | None = 0

    def __post_init__(self):
        self.pickcode = self.pickcode or f"pc-{self.fid}"


@dataclass
class _CloudFS:
    dirs: dict = field(default_factory=dict)
    files: dict[str, _File] = field(default_factory=dict)
    copy_calls: list = field(default_factory=list)
    rename_calls: list = field(default_factory=list)
    delete_calls: list = field(default_factory=list)
    # 注入指定 fid 让 delete_files 抛错，用于验证 cleanup-source 删源失败可重试。
    delete_fail_fids: set[str] = field(default_factory=set)
    # 按 sha1 拦 copy / rename，方便测 rollback 分支（分层布局下 fid 依赖 mkdir 递增，
    # 难以直接猜准）。
    copy_fail_sha1s: set[str] = field(default_factory=set)
    rename_fail_fids: set[str] = field(default_factory=set)
    counter: itertools.count = field(default_factory=lambda: itertools.count(100))

    def add_dir(self, cid, name, parent):
        self.dirs[cid] = {"name": name, "parent": parent}

    def add_file(self, **kwargs):
        item = _File(**kwargs)
        self.files[item.fid] = item
        return item

    async def dir_info(self, cid):
        if cid == "0":
            return DirMeta(cid="0", name="根目录", pickcode="", parent_id="", file_count=0,
                           folder_count=0, play_long_seconds=0, mtime=0, ctime=0, paths=())
        crumbs = []
        current = self.dirs[cid]["parent"]
        while current != "0":
            crumbs.append(DirBreadcrumb(file_id=current, name=self.dirs[current]["name"]))
            current = self.dirs[current]["parent"]
        crumbs.append(DirBreadcrumb(file_id="0", name="根目录"))
        return DirMeta(cid=cid, name=self.dirs[cid]["name"], pickcode="",
                       parent_id=self.dirs[cid]["parent"], file_count=0, folder_count=0,
                       play_long_seconds=0, mtime=0, ctime=0, paths=tuple(reversed(crumbs)))

    def _dir_entry(self, cid):
        info = self.dirs[cid]
        return DirEntry(entry_id=cid, parent_id=info["parent"], name=info["name"],
                        is_dir=True, size=0, sha1=None, pickcode="", mtime=0, ctime=0,
                        is_video=False)

    def _file_entry(self, item):
        return DirEntry(entry_id=item.fid, parent_id=item.parent, name=item.name,
                        is_dir=False, size=item.size, sha1=item.sha1,
                        pickcode=item.pickcode, mtime=0, ctime=0, is_video=True,
                        play_long=item.play_long, ic=item.ic)

    async def list_dir(self, cid, *, offset=0, limit=1000):
        entries = [self._dir_entry(key) for key, value in self.dirs.items() if value["parent"] == cid]
        entries += [self._file_entry(item) for item in self.files.values() if item.parent == cid]
        return entries[offset:offset + limit], len(entries)

    def _scope(self, cid):
        result = {cid}
        changed = True
        while changed:
            changed = False
            for key, value in self.dirs.items():
                if value["parent"] in result and key not in result:
                    result.add(key)
                    changed = True
        return result

    async def iter_files_recursive(self, cid, *, page_size=1000):
        scope = self._scope(cid)
        for item in list(self.files.values()):
            if item.parent in scope:
                yield self._file_entry(item)

    async def mkdir(self, pid, name):
        cid = f"dir-{next(self.counter)}"
        self.add_dir(cid, name, pid)
        return cid

    async def copy_files(self, fids, *, pid):
        self.copy_calls.append((tuple(fids), pid))
        for fid in fids:
            source = self.files[fid]
            if source.sha1 in self.copy_fail_sha1s:
                raise RuntimeError(f"simulated copy failure sha1={source.sha1}")
            copied_fid = f"{fid}-copy-{next(self.counter)}"
            self.add_file(fid=copied_fid, parent=pid, name=source.name, sha1=source.sha1,
                          size=source.size, play_long=source.play_long, ic=source.ic)

    async def rename_file(self, fid, new_name):
        self.rename_calls.append((fid, new_name))
        if fid in self.rename_fail_fids:
            raise RuntimeError(f"simulated rename failure fid={fid}")
        self.files[fid].name = new_name

    async def file_info(self, fid):
        if fid not in self.files:
            raise Cloud115NotFoundError(f"missing fid={fid}")
        item = self.files[fid]
        return FileMeta(file_id=item.fid, parent_id=item.parent, name=item.name,
                        size=item.size, sha1=item.sha1, pickcode=item.pickcode,
                        mtime=0, ctime=0, is_video=True)

    async def delete_files(self, fids, *, pid=None):
        self.delete_calls.append(tuple(fids))
        for fid in fids:
            if fid in self.delete_fail_fids:
                raise RuntimeError(f"simulated delete failure fid={fid}")
        for fid in fids:
            self.files.pop(fid, None)

    async def get_download_url(self, pickcode, user_agent):
        item = next(item for item in self.files.values() if item.pickcode == pickcode)
        return DirectUrl(file_id=item.fid, file_name=item.name, file_size=item.size,
                         sha1=item.sha1, pickcode=item.pickcode,
                         url=f"https://cdn.invalid/{item.fid}", user_agent=user_agent,
                         expires_at=-1)


class _Probe:
    def __init__(self, result=None):
        self.result = result or MediaMetadataProbeResult(
            resolution="3840x2160",
            duration_seconds=100,
            creation_time=datetime(2024, 1, 2, 3, 4),
            video_info={
                "container": {"format_name": "mp4", "duration_seconds": 100},
                "video": {"codec_name": "hevc", "width": 3840, "height": 2160},
                "audio": None,
                "subtitles": [],
            },
        )
        self.calls = 0

    def probe_source(self, source, *, file_size_bytes, source_label):
        self.calls += 1
        return self.result


@pytest.fixture()
def cloud_video_tables(test_db):
    models = [
        Image, MovieSeries, Movie, VideoItem, VideoCollection, VideoCollectionItem,
        MediaLibrary, DownloadClient, Indexer, DownloadTask, BackgroundTaskRun,
        ImportJob, VideoImportJob, Media, ResourceTaskState,
        SystemEvent, SystemNotification,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db


@pytest.fixture()
def cloud_setup(cloud_video_tables, monkeypatch):
    fs = _CloudFS()
    fs.add_dir("lib", "sakuramedia", "0")
    fs.add_dir("source", "来源", "0")
    library = MediaLibrary.create(
        name="cloud-videos",
        backend="cloud115",
        backend_account_key="cloud115:test-videos",
        backend_config={"cookies": "UID=1_A1_1; CID=x; SEID=y", "root_cid": "lib", "app": "web"},
    )

    @asynccontextmanager
    async def fake_client_for(_library):
        yield fs

    monkeypatch.setattr(service_module, "cloud115_client_for", fake_client_for)
    monkeypatch.setattr(job_module, "cloud115_client_for", fake_client_for)
    monkeypatch.setattr(jav_job_module, "cloud115_client_for", fake_client_for)
    monkeypatch.setattr(service_module.VideoCoverService, "generate_cover", lambda video, source: None)
    return fs, library


def test_directory_copy_import_writes_video_info_and_collection(cloud_setup):
    fs, library = cloud_setup
    fs.add_dir("nested", "子目录", "source")
    source = fs.add_file(fid="f1", parent="nested", name="clip-4k.mp4", sha1="AAA")
    collection = VideoCollection.create(name="合集")
    probe = _Probe()

    job = Cloud115VideoImportService(probe).import_from_cloud115(
        library.id, source_cid="source", collection_id=collection.id
    )

    media = Media.get()
    video = VideoItem.get()
    assert job.state == "completed"
    assert job.imported_count == 1
    assert source.fid in fs.files
    assert media.video_item_id == video.id and media.movie_number is None
    assert media.video_info["video"]["codec_name"] == "hevc"
    assert media.resolution == "3840x2160"
    assert media.special_tags == "4K"
    assert video.release_date == datetime(2024, 1, 2, 3, 4)
    assert VideoCollectionItem.get().video_item_id == video.id
    assert probe.calls == 1


def test_cleanup_source_deletes_only_after_success(cloud_setup):
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    job = Cloud115VideoImportService(_Probe()).import_from_cloud115(
        library.id, source_cid="source", transfer_mode="cleanup-source"
    )
    assert job.state == "completed"
    assert "f1" not in fs.files
    assert fs.delete_calls == [("f1",)]


def test_probe_failure_keeps_source_and_does_not_register(cloud_setup):
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    probe = _Probe(MediaMetadataProbeResult())
    job = Cloud115VideoImportService(probe).import_from_cloud115(
        library.id, source_cid="source", transfer_mode="cleanup-source"
    )
    assert job.state == "failed"
    assert "cloud115_metadata_probe_failed" in job.failed_files
    assert VideoItem.select().count() == 0
    assert Media.select().count() == 0
    assert "f1" in fs.files
    assert fs.delete_calls == []


def test_duplicate_cleanup_source_still_deletes_remote_source(cloud_setup):
    """已入库内容再次 cleanup-source 导入时，仍要删远端源文件（否则源永久滞留 115）。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    service = Cloud115VideoImportService(_Probe())
    # 第一次 copy 模式：入库并保留源文件。
    service.import_from_cloud115(library.id, source_cid="source")
    assert "f1" in fs.files
    # 第二次 cleanup-source：不重登，但必须清源。
    job = service.import_from_cloud115(
        library.id, source_cid="source", transfer_mode="cleanup-source"
    )
    assert job.state == "completed"
    assert job.skipped_count == 1
    assert Media.select().count() == 1  # 未重登
    assert "f1" not in fs.files
    assert ("f1",) in fs.delete_calls


def test_duplicate_cleanup_source_delete_failure_is_retryable(cloud_setup):
    """cleanup-source 删源失败 → 失败项 kind=file，允许用户通过失败列表精确重试。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    service = Cloud115VideoImportService(_Probe())
    service.import_from_cloud115(library.id, source_cid="source")

    fs.delete_fail_fids.add("f1")
    job = service.import_from_cloud115(
        library.id, source_cid="source", transfer_mode="cleanup-source"
    )

    assert job.state == "failed"
    assert "f1" in fs.files
    # job.failed_files 是 JSON dump 的字符串（VideoImportJob 存 text 列）；反序列后按 reason 过滤。
    failed = [
        item for item in json.loads(job.failed_files)
        if item.get("reason") == "source_delete_failed"
    ]
    matching = [item for item in failed if item.get("path") == "clip.mp4"]
    assert matching, f"expected clip.mp4 in source_delete_failed, got {job.failed_files}"
    # 关键：kind=file 才会进入可重试白名单，warning 不会。
    assert matching[0].get("kind") == "file"


def test_duplicate_copy_mode_skips_and_keeps_source(cloud_setup):
    """copy 模式下已存在内容仍然跳过，且不动源文件（保持向后兼容）。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    service = Cloud115VideoImportService(_Probe())
    service.import_from_cloud115(library.id, source_cid="source")
    fs.delete_calls.clear()

    job = service.import_from_cloud115(library.id, source_cid="source")

    assert job.state == "completed"
    assert job.skipped_count == 1
    assert "f1" in fs.files
    assert fs.delete_calls == []


def test_single_file_source_and_censored_file_skips_probe(cloud_setup):
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="blocked.mp4", sha1="AAA", ic=1)
    probe = _Probe()
    job = Cloud115VideoImportService(probe).import_from_cloud115(
        library.id, source_fid="f1"
    )
    media = Media.get()
    assert job.state == "completed"
    assert media.valid is False
    assert media.video_info is None
    assert probe.calls == 0
    assert "cloud115_file_censored" in job.failed_files


def test_cloud_job_trigger_persists_source_and_shares_jav_mutex(cloud_setup, monkeypatch):
    fs, library = cloud_setup
    monkeypatch.setattr(DownloadImportRunner, "submit", staticmethod(lambda *args, **kwargs: None))

    result = Cloud115VideoImportJobService.trigger_cloud115_import(
        library.id, source_cid="source"
    )
    job = VideoImportJob.get_by_id(result.video_import_job_id)
    assert job.source_cid == "source"
    assert job.source_fid is None
    assert job.transfer_mode == "copy"
    assert job.task_run.mutex_key == f"media_import:cloud115:{library.id}"

    with pytest.raises(ApiError) as exc_info:
        Cloud115ImportJobService.trigger_cloud115_import(
            library.id, "source", transfer_mode="copy"
        )
    assert exc_info.value.code == "media_import_conflict"


def test_cloud_failed_file_rename_and_delete(cloud_setup):
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="broken.mp4", sha1="AAA")
    job = VideoImportJob.create(
        source_path="根目录/来源",
        source_cid="source",
        library=library,
        state="failed",
        failed_count=1,
        failed_files='[{"path":"broken.mp4","reason":"media_import_failed","detail":"x","kind":"file"}]',
    )

    renamed = Cloud115VideoImportJobService.rename_failed_file(
        job.id, "broken.mp4", "fixed.mp4"
    )
    assert renamed.failed_files[0].path == "fixed.mp4"
    assert fs.files["f1"].name == "fixed.mp4"

    deleted = Cloud115VideoImportJobService.delete_failed_file(job.id, "fixed.mp4")
    assert deleted.failed_files == []
    assert "f1" not in fs.files


def test_directory_import_is_stably_sorted_and_has_no_minimum_size(cloud_setup):
    fs, library = cloud_setup
    fs.add_file(fid="f2", parent="source", name="b.mp4", sha1="BBB", size=1)
    fs.add_file(fid="ignored", parent="source", name="note.txt", sha1="TXT", size=1)
    fs.add_file(fid="f1", parent="source", name="a.mp4", sha1="AAA", size=1)
    collection = VideoCollection.create(name="顺序合集")

    job = Cloud115VideoImportService(_Probe()).import_from_cloud115(
        library.id, source_cid="source", collection_id=collection.id
    )

    items = list(
        VideoCollectionItem.select()
        .where(VideoCollectionItem.collection == collection)
        .order_by(VideoCollectionItem.position)
    )
    assert job.imported_count == 2
    assert [item.video_item.title for item in items] == ["a", "b"]
    assert [item.position for item in items] == [0, 1]


def test_probe_failure_avoids_copy_and_retry_uses_source_probe(cloud_setup):
    """probe 使用**源 pickcode**（无需先落到受管目录才能读），probe 失败根本不进 copy；
    重跑时 probe 成功再正常复制。此前 probe 走 target pickcode，必须先 copy 才能 probe，
    现在改为源 probe 后省去这条"先 copy 才可能失败"的路径。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    failed = Cloud115VideoImportService(
        _Probe(MediaMetadataProbeResult())
    ).import_from_cloud115(library.id, source_cid="source")
    assert failed.state == "failed"
    # 关键差异：probe 失败在 copy 之前，copy_calls 保持为空。
    assert fs.copy_calls == []
    assert VideoItem.select().count() == 0

    retried = Cloud115VideoImportService(_Probe()).import_from_cloud115(
        library.id, source_cid="source"
    )
    assert retried.state == "completed"
    assert retried.imported_count == 1
    assert len(fs.copy_calls) == 1


def test_zero_probed_duration_falls_back_to_play_long(cloud_setup):
    fs, library = cloud_setup
    fs.add_file(
        fid="f1", parent="source", name="clip.mp4", sha1="AAA", play_long=321
    )
    probe = _Probe(
        MediaMetadataProbeResult(
            resolution="1920x1080",
            duration_seconds=0,
            video_info={
                "container": {"format_name": "mp4", "duration_seconds": 0},
                "video": {"codec_name": "h264", "width": 1920, "height": 1080},
                "audio": None,
                "subtitles": [],
            },
        )
    )
    Cloud115VideoImportService(probe).import_from_cloud115(
        library.id, source_cid="source"
    )
    assert Media.get().duration_seconds == 321
    assert VideoItem.get().release_date is None


def test_single_file_delete_is_idempotent_when_source_is_missing(cloud_setup):
    _fs, library = cloud_setup
    job = VideoImportJob.create(
        source_path="根目录/来源/missing.mp4",
        source_fid="missing-fid",
        library=library,
        state="failed",
        failed_count=1,
        failed_files=(
            '[{"path":"missing.mp4","reason":"media_import_failed",'
            '"detail":"x","kind":"file"}]'
        ),
    )

    result = Cloud115VideoImportJobService.delete_failed_file(
        job.id, "missing.mp4"
    )

    assert result.failed_files == []


def test_failed_file_write_operation_uses_shared_cloud_mutex(cloud_setup):
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="broken.mp4", sha1="AAA")
    job = VideoImportJob.create(
        source_path="根目录/来源",
        source_cid="source",
        library=library,
        state="failed",
        failed_count=1,
        failed_files=(
            '[{"path":"broken.mp4","reason":"media_import_failed",'
            '"detail":"x","kind":"file"}]'
        ),
    )
    blocker = BackgroundTaskRun.create(
        task_key="video_directory_import",
        task_name="blocking JAV import",
        trigger_type="manual",
        state="running",
        mutex_key=f"media_import:cloud115:{library.id}",
    )

    with pytest.raises(ApiError) as exc_info:
        Cloud115VideoImportJobService.delete_failed_file(job.id, "broken.mp4")

    assert exc_info.value.code == "video_import_conflict"
    assert exc_info.value.details["blocking_task_run_id"] == blocker.id
    assert "f1" in fs.files


@pytest.mark.parametrize("mode", ["auto", "move", "invalid"])
def test_cloud_video_trigger_rejects_non_cloud_modes(mode):
    with pytest.raises(ApiError) as exc_info:
        Cloud115VideoImportJobService.trigger_cloud115_import(
            999, source_cid="source", transfer_mode=mode
        )
    assert exc_info.value.code == "invalid_transfer_mode"


# ---------------------------------------------------------------------------
# rollback：任一失败分支都要清 VideoItem + 已 copy 文件 + version_cid + entity_cid
# 回归 code review 结论：VideoItem 若跨这些失败不清理会永久孤儿（本地 VideoImportService
# 的语义是失败即回滚整个 video），本次修复对齐同一语义。
# ---------------------------------------------------------------------------


def test_mkdir_failure_rolls_back_video_item(cloud_setup, monkeypatch):
    """建目标目录失败：VideoItem 已入库、云端啥都还没建，也要 delete_instance 回收。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")

    original_mkdir = fs.mkdir

    async def fail_after_first(pid, name):
        # 建到 videos/ 之后就抛，模拟建 {video_id} 或 {版本ms} 时上游 500。
        if name.isdigit() or name == "videos":
            if name == "videos":
                return await original_mkdir(pid, name)
            raise RuntimeError("simulated mkdir failure")
        return await original_mkdir(pid, name)

    fs.mkdir = fail_after_first  # type: ignore[assignment]

    job = Cloud115VideoImportService(_Probe()).import_from_cloud115(
        library.id, source_cid="source"
    )

    assert job.state == "failed"
    assert "cloud115_transfer_failed" in job.failed_files
    assert VideoItem.select().count() == 0
    assert Media.select().count() == 0


def test_copy_failure_rolls_back_video_item_and_dirs(cloud_setup):
    """copy_files 失败：已建的 version_cid + entity_cid 都要回收，VideoItem 也 delete。
    entity_cid（videos/{video_id}/）是本次导入独占，可以放心删。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    fs.copy_fail_sha1s.add("AAA")

    job = Cloud115VideoImportService(_Probe()).import_from_cloud115(
        library.id, source_cid="source"
    )

    assert job.state == "failed"
    assert "cloud115_transfer_failed" in job.failed_files
    assert VideoItem.select().count() == 0
    assert Media.select().count() == 0
    # 建过的 version + entity 目录都应出现在 delete_calls 里。
    entity_dirs = [cid for cid, info in fs.dirs.items() if info["parent"] and fs.dirs.get(info["parent"], {}).get("name") == "videos"]
    version_dirs = [cid for cid, info in fs.dirs.items() if info["parent"] in entity_dirs]
    deleted = {fid for call in fs.delete_calls for fid in call}
    for cid in entity_dirs + version_dirs:
        assert cid in deleted, f"expected {cid} rolled back but delete_calls={fs.delete_calls}"


def test_rename_failure_rolls_back_all_resources(cloud_setup):
    """rename 失败：已 copy 的文件、version_cid、entity_cid、VideoItem 全都要清。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="raw.mp4", sha1="AAA")
    # 让复制产物 fid = f1-copy-... 的 rename 抛错，需要按复制后 fid 拦。
    # 但复制后 fid 由 counter 递增无法预测；改法：monkeypatch rename_file 让它总抛错。
    fs.rename_calls.clear()
    original_rename = fs.rename_file

    async def failing_rename(fid, new_name):
        fs.rename_calls.append((fid, new_name))
        raise RuntimeError("simulated rename failure")

    fs.rename_file = failing_rename  # type: ignore[assignment]
    # 触发 rename 分支：源名 raw.mp4 ≠ target_name（videos 域直接用 source.name）
    # 需要让 target_entry.name != source.name → 手动让 copy 后 name 变异。
    original_copy = fs.copy_files

    async def rename_after_copy(fids, *, pid):
        await original_copy(fids, pid=pid)
        # 让复制产物名变异触发 rename 分支
        for fid in fids:
            copied = next(f for f in fs.files.values() if f.parent == pid and f.sha1 == fs.files[fid].sha1)
            copied.name = "mangled-by-115.mp4"

    fs.copy_files = rename_after_copy  # type: ignore[assignment]

    job = Cloud115VideoImportService(_Probe()).import_from_cloud115(
        library.id, source_cid="source"
    )

    assert job.state == "failed"
    assert "cloud115_rename_failed" in job.failed_files
    assert VideoItem.select().count() == 0
    assert Media.select().count() == 0
    # copy 出的文件应被清（fid 未知，但 sha1=AAA 且父目录不在源目录 source 下的文件应已消失）
    remaining_aaa_off_source = [
        f for f in fs.files.values() if f.sha1 == "AAA" and f.parent != "source"
    ]
    assert remaining_aaa_off_source == []
    # entity 与 version 目录也应被清
    entity_dirs = [cid for cid, info in fs.dirs.items() if info["parent"] and fs.dirs.get(info["parent"], {}).get("name") == "videos"]
    deleted = {fid for call in fs.delete_calls for fid in call}
    for cid in entity_dirs:
        assert cid in deleted


def test_rollback_failures_only_warn_and_dont_break_job_state(cloud_setup):
    """rollback 里各 delete 失败仅告警，不改变 job 最终状态（对齐本地 VideoImportService）。"""
    fs, library = cloud_setup
    fs.add_file(fid="f1", parent="source", name="clip.mp4", sha1="AAA")
    fs.copy_fail_sha1s.add("AAA")
    # 让所有 delete 都失败：无论 rollback 遇到什么，都只 warning、不再抛。
    original_delete = fs.delete_files

    async def always_fail_delete(fids, *, pid=None):
        fs.delete_calls.append(tuple(fids))
        raise RuntimeError("rollback delete crashed")

    fs.delete_files = always_fail_delete  # type: ignore[assignment]

    job = Cloud115VideoImportService(_Probe()).import_from_cloud115(
        library.id, source_cid="source"
    )

    # job 收敛为 failed（因为 copy_fail 触发的失败原因），VideoItem 是否被删由 delete_instance
    # 决定（DB 操作不走 delete_files）—— 断言不出崩溃即达标。
    assert job.state == "failed"
    assert "cloud115_transfer_failed" in job.failed_files
    # VideoItem 通过 peewee delete_instance 清理，不依赖 delete_files。
    assert VideoItem.select().count() == 0
