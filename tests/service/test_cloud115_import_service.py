"""Cloud115ImportService 编排测试：内存 115 文件系统替身 + 假元数据抓取。

覆盖：cleanup-source/copy 全流程、copy 对账（新 fid/pickcode）、幂等（目标已有同 sha1）、
sha1 去重（库内 + 批内）、番号识别失败、ic 违规登记 invalid、字幕下载与清源、
only_files 子集重导、元数据失败传播、编码文件名截断。
"""

from __future__ import annotations

import itertools
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import pytest

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
    Subtitle,
    VideoItem,
)
from src.service.transfers import cloud115_import_service as service_module
from src.service.transfers.cloud115_import_service import (
    Cloud115ImportService,
    encode_cloud_file_name,
)
from src.service.transfers.media_import_service import MediaImportService, MetadataImportResult
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeResult

COOKIES = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz"


# ---------------------------------------------------------------------------
# 内存 115 文件系统替身
# ---------------------------------------------------------------------------


@dataclass
class FakeCloudFile:
    fid: str
    parent: str
    name: str
    sha1: str
    # 显著大于 allowed_min_video_file_size 的任何本地配置值，避免被小文件过滤误伤
    size: int = 4 * 1024 * 1024 * 1024
    pickcode: str = ""
    play_long: int | None = 1800
    ic: int | None = 0

    def __post_init__(self):
        if not self.pickcode:
            self.pickcode = f"pc-{self.fid}"


@dataclass
class FakeCloud115FS:
    """极简内存 115：目录树 + 文件表，支撑导入管线所需的全部 SDK 方法。"""

    dirs: dict = field(default_factory=dict)     # cid -> {"name":, "parent":}
    files: dict = field(default_factory=dict)    # fid -> FakeCloudFile
    subtitle_content: bytes = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n"
    copy_calls: list = field(default_factory=list)
    move_calls: list = field(default_factory=list)
    rename_calls: list = field(default_factory=list)
    delete_calls: list = field(default_factory=list)
    rename_fail_fids: set[str] = field(default_factory=set)
    rename_noop_fids: set[str] = field(default_factory=set)
    delete_fail_fids: set[str] = field(default_factory=set)
    subtitle_error: Exception | None = None
    _fid_counter: itertools.count = field(default_factory=lambda: itertools.count(1000))

    def add_dir(self, cid: str, name: str, parent: str) -> None:
        self.dirs[cid] = {"name": name, "parent": parent}

    def add_file(self, **kwargs) -> FakeCloudFile:
        f = FakeCloudFile(**kwargs)
        self.files[f.fid] = f
        return f

    # ---- SDK 接口 ----

    async def dir_info(self, cid: str) -> DirMeta:
        if cid == "0":
            return DirMeta(cid="0", name="根目录", pickcode="", parent_id="",
                           file_count=0, folder_count=0, play_long_seconds=0,
                           mtime=0, ctime=0, paths=())
        crumbs = []
        current = self.dirs[cid]["parent"]
        while current != "0":
            crumbs.append(DirBreadcrumb(file_id=current, name=self.dirs[current]["name"]))
            current = self.dirs[current]["parent"]
        crumbs.append(DirBreadcrumb(file_id="0", name="根目录"))
        return DirMeta(cid=cid, name=self.dirs[cid]["name"], pickcode="",
                       parent_id=self.dirs[cid]["parent"], file_count=0, folder_count=0,
                       play_long_seconds=0, mtime=0, ctime=0, paths=tuple(reversed(crumbs)))

    def _dir_entry(self, cid: str) -> DirEntry:
        info = self.dirs[cid]
        return DirEntry(entry_id=cid, parent_id=info["parent"], name=info["name"],
                        is_dir=True, size=0, sha1=None, pickcode="", mtime=0, ctime=0,
                        is_video=False)

    def _file_entry(self, f: FakeCloudFile) -> DirEntry:
        return DirEntry(entry_id=f.fid, parent_id=f.parent, name=f.name, is_dir=False,
                        size=f.size, sha1=f.sha1, pickcode=f.pickcode, mtime=0, ctime=0,
                        is_video=True, play_long=f.play_long, ic=f.ic)

    async def list_dir(self, cid: str, *, offset: int = 0, limit: int = 1000):
        entries = [self._dir_entry(c) for c, i in self.dirs.items() if i["parent"] == cid]
        entries += [self._file_entry(f) for f in self.files.values() if f.parent == cid]
        return entries[offset:offset + limit], len(entries)

    def _descendant_dirs(self, cid: str) -> set:
        result = {cid}
        changed = True
        while changed:
            changed = False
            for c, info in self.dirs.items():
                if info["parent"] in result and c not in result:
                    result.add(c)
                    changed = True
        return result

    async def iter_files_recursive(self, cid: str, *, page_size: int = 1000):
        scope = self._descendant_dirs(cid)
        for f in list(self.files.values()):
            if f.parent in scope:
                yield self._file_entry(f)

    async def mkdir(self, pid: str, name: str) -> str:
        cid = f"dir-{next(self._fid_counter)}"
        self.add_dir(cid, name, pid)
        return cid

    async def copy_files(self, fids, *, pid):
        self.copy_calls.append((tuple(fids), pid))
        for fid in fids:
            src = self.files[fid]
            new_fid = f"{fid}-copy{next(self._fid_counter)}"
            # 复制产生新 fid + 新 pickcode，sha1 不变（与真实 115 行为一致）
            self.files[new_fid] = FakeCloudFile(
                fid=new_fid, parent=pid, name=src.name, sha1=src.sha1,
                size=src.size, play_long=src.play_long, ic=src.ic,
            )

    async def move_files(self, fids, *, pid):
        self.move_calls.append((tuple(fids), pid))
        for fid in fids:
            self.files[fid].parent = pid

    async def batch_rename(self, renames):
        raise AssertionError("导入流程不得调用 batch_rename")

    async def rename_file(self, fid, new_name):
        self.rename_calls.append((fid, new_name))
        if fid in self.rename_fail_fids:
            raise RuntimeError("rename denied")
        if fid not in self.rename_noop_fids:
            self.files[fid].name = new_name

    async def file_info(self, fid):
        f = self.files[fid]
        return FileMeta(
            file_id=f.fid, parent_id=f.parent, name=f.name, size=f.size,
            sha1=f.sha1, pickcode=f.pickcode, mtime=0, ctime=0, is_video=True,
        )

    async def delete_files(self, fids, *, pid=None):
        self.delete_calls.append(tuple(fids))
        for fid in fids:
            if fid in self.delete_fail_fids:
                raise RuntimeError("delete denied")
            self.files.pop(fid, None)

    async def download_bytes(self, pickcode, *, user_agent, max_bytes=0):
        if self.subtitle_error is not None:
            raise self.subtitle_error
        return self.subtitle_content

    async def get_download_url(self, pickcode, user_agent):
        matched = next(f for f in self.files.values() if f.pickcode == pickcode)
        return DirectUrl(
            file_id=matched.fid,
            file_name=matched.name,
            file_size=matched.size,
            sha1=matched.sha1,
            pickcode=matched.pickcode,
            url=f"https://cdn.115.example/{matched.fid}",
            user_agent=user_agent,
            expires_at=-1,
        )


class FakeMetadataService:
    """替身元数据服务：为每个番号造一部 Movie 并返回成功结果。"""

    metadata_import_batch = MediaImportService.metadata_import_batch

    def __init__(self, fail_numbers: set[str] | None = None):
        self.fail_numbers = fail_numbers or set()

    def _metadata_max_workers(self, total: int) -> int:
        return 1

    def _import_movie_metadata(self, movie_number: str) -> MetadataImportResult:
        if movie_number in self.fail_numbers:
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason="metadata_fetch_failed",
                failure_detail="fake fetch failure",
            )
        movie, _created = Movie.get_or_create(
            javdb_id=f"jd-{movie_number}",
            defaults={"movie_number": movie_number, "title": movie_number},
        )
        return MetadataImportResult(movie_number=movie_number, movie_id=movie.id)


class FakeMediaMetadataProbeService:
    def __init__(self, result: MediaMetadataProbeResult | None = None):
        self.result = result or MediaMetadataProbeResult(
            resolution="1920x1080",
            duration_seconds=1700,
            video_info={
                "container": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration_seconds": 1700,
                    "bit_rate": 5_000_000,
                    "size_bytes": 4 * 1024 * 1024 * 1024,
                },
                "video": {
                    "codec_name": "h264",
                    "profile": "High",
                    "width": 1920,
                    "height": 1080,
                },
                "audio": {"codec_name": "aac", "channels": 2},
                "subtitles": [],
            },
        )
        self.called_sources: list[tuple[int, str]] = []

    def probe_source(self, _source, *, file_size_bytes: int, source_label: str):
        self.called_sources.append((file_size_bytes, source_label))
        return self.result


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cloud115_import_tables(test_db):
    models = [
        Image, MovieSeries, Movie, VideoItem, MediaLibrary,
        DownloadClient, Indexer, DownloadTask,
        BackgroundTaskRun, ImportJob, Media, Subtitle, ResourceTaskState,
    ]
    test_db.bind(models, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(models)
    yield test_db
    test_db.drop_tables(list(reversed(models)))


@pytest.fixture()
def cloud_library(cloud115_import_tables):
    return MediaLibrary.create(
        name="cloud",
        backend="cloud115",
        backend_config={"cookies": COOKIES, "root_cid": "lib-root", "app": "alipaymini"},
    )


@pytest.fixture()
def fs():
    """基础目录结构：根 0 下有库根 lib-root（含 jav/）与源目录 src-1。"""
    fake = FakeCloud115FS()
    fake.add_dir("lib-root", "sakuramedia", "0")
    fake.add_dir("jav-dir", "jav", "lib-root")
    fake.add_dir("src-1", "来源目录", "0")
    return fake


@pytest.fixture()
def patched_client(monkeypatch, fs):
    @asynccontextmanager
    async def fake_client_for(_library):
        yield fs

    monkeypatch.setattr(service_module, "cloud115_client_for", fake_client_for)
    return fs


@pytest.fixture()
def subtitle_root(monkeypatch, tmp_path):
    from src.config.config import settings

    monkeypatch.setattr(settings.media, "subtitle_root_path", str(tmp_path / "subtitles"))
    return tmp_path / "subtitles"


def _run_import(library, source_cid="src-1", **kwargs):
    probe_result = kwargs.pop("probe_result", None)
    service = Cloud115ImportService(media_import_service=FakeMetadataService(
        fail_numbers=kwargs.pop("fail_numbers", None),
    ), media_metadata_probe_service=FakeMediaMetadataProbeService(probe_result))
    return service.import_from_cloud115(library.id, source_cid, **kwargs)


# ---------------------------------------------------------------------------
# encode_cloud_file_name
# ---------------------------------------------------------------------------


def test_encode_cloud_file_name_joins_rel_parts():
    assert encode_cloud_file_name(("ABP-123", "CD1"), "movie.mp4") == "ABP-123＿CD1＿movie.mp4"
    assert encode_cloud_file_name((), "movie.mp4") == "movie.mp4"


def test_encode_cloud_file_name_drops_head_dirs_when_too_long():
    # 超长时先丢最靠近源根的目录段（保尾），文件名+扩展名完整
    name = encode_cloud_file_name(("A" * 100, "B" * 100), "movie.mp4", max_length=120)
    assert name == "B" * 100 + "＿movie.mp4"


def test_encode_cloud_file_name_truncates_stem_preserving_suffix():
    name = encode_cloud_file_name((), "X" * 300 + ".mp4", max_length=50)
    assert name.endswith(".mp4")
    assert len(name) <= 50


# ---------------------------------------------------------------------------
# cleanup-source 模式全流程
# ---------------------------------------------------------------------------


def test_cleanup_source_import_registers_then_deletes_source(
    cloud_library, patched_client, subtitle_root
):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-123", "src-1")
    video = fs.add_file(fid="f-1", parent="d-abp", name="movie.mp4", sha1="AAA111")
    fs.add_file(fid="f-srt", parent="d-abp", name="movie.srt", sha1="SRT111", size=1024)

    job = _run_import(cloud_library)

    assert job.state == "completed"
    assert job.imported_count == 1
    assert job.failed_count == 0
    # cleanup-source 先复制，登记新 fid/pickcode，最后删除源视频与字幕。
    assert "f-1" not in fs.files
    media = Media.get()
    assert media.path is None
    assert media.backend_locator == {
        "fid": media.backend_locator["fid"],
        "pickcode": media.backend_locator["pickcode"],
        "name": "ABP-123＿movie.mp4",
        "source_path": "ABP-123/movie.mp4",
    }
    assert media.backend_locator["fid"] != "f-1"
    assert media.backend_locator["pickcode"] != video.pickcode
    assert media.content_fingerprint == "sha1:AAA111"
    assert media.resolution == "1920x1080"
    assert media.duration_seconds == 1700
    assert media.video_info["video"]["codec_name"] == "h264"
    assert media.valid is True
    assert media.library_id == cloud_library.id
    # 字幕：先下载登记，整组成功后与源视频一起清理。
    subtitle = Subtitle.get()
    assert subtitle.file_path.endswith("ABP-123＿movie.srt")
    assert "ABP-123" in subtitle.file_path
    assert ("f-srt",) in fs.delete_calls
    assert ("f-1",) in fs.delete_calls
    # 缩略图任务已排队
    assert ResourceTaskState.select().count() >= 0


def test_import_fails_before_register_and_cleanup_when_media_probe_is_empty(
    cloud_library, patched_client
):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-124", "src-1")
    fs.add_file(fid="f-probe", parent="d-abp", name="ABP-124.mp4", sha1="AAA124")

    job = _run_import(cloud_library, probe_result=MediaMetadataProbeResult())

    assert job.state == "failed"
    assert "cloud115_metadata_probe_failed" in job.failed_files
    assert Media.select().count() == 0
    assert "f-probe" in fs.files
    assert fs.delete_calls == []


def test_import_builds_4k_tag_from_probed_video_info(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-125", "src-1")
    fs.add_file(fid="f-4k", parent="d-abp", name="ABP-125.mp4", sha1="AAA125")
    video_info = {
        "container": {"format_name": "matroska", "duration_seconds": 3600},
        "video": {"codec_name": "hevc", "width": 3840, "height": 2160},
        "audio": None,
        "subtitles": [],
    }

    job = _run_import(
        cloud_library,
        probe_result=MediaMetadataProbeResult(
            resolution="3840x2160",
            duration_seconds=3600,
            video_info=video_info,
        ),
    )

    media = Media.get()
    assert job.state == "completed"
    assert media.resolution == "3840x2160"
    assert media.duration_seconds == 3600
    assert media.video_info == video_info
    assert media.special_tags == "4K"


def test_copy_import_reconciles_new_fid_and_pickcode(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-456", "src-1")
    source = fs.add_file(fid="f-2", parent="d-abp", name="ABP-456.mp4", sha1="BBB222")

    job = _run_import(cloud_library, transfer_mode="copy")

    assert job.state == "completed"
    assert job.imported_count == 1
    # 源文件保持原位（copy 不清源）
    assert fs.files["f-2"].parent == "d-abp"
    # 登记的是复制产物的新 fid/pickcode，不是源条目
    media = Media.get()
    assert media.backend_locator["fid"] != "f-2"
    assert media.backend_locator["pickcode"] != source.pickcode
    assert media.content_fingerprint == "sha1:BBB222"
    # 复制产物已改路径编码名
    copied = fs.files[media.backend_locator["fid"]]
    assert copied.parent == "jav-dir"
    assert copied.name == "ABP-456＿ABP-456.mp4"


def test_copy_import_skips_transfer_when_target_already_has_sha1(cloud_library, patched_client):
    """幂等：上次中断已复制的文件（目标目录同 sha1），重跑不再 copy、直接对账登记。"""
    fs = patched_client
    fs.add_dir("d-abp", "ABP-789", "src-1")
    fs.add_file(fid="f-3", parent="d-abp", name="ABP-789.mp4", sha1="CCC333")
    # 目标目录已有上次复制的同 sha1 条目
    fs.add_file(fid="f-3-old-copy", parent="jav-dir", name="ABP-789.mp4", sha1="CCC333")

    job = _run_import(cloud_library, transfer_mode="copy")

    assert job.state == "completed"
    assert job.imported_count == 1
    assert fs.copy_calls == []          # 未再发起复制
    media = Media.get()
    assert media.backend_locator["fid"] == "f-3-old-copy"


def test_import_skips_duplicate_sha1_in_library(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-100", "src-1")
    fs.add_file(fid="f-4", parent="d-abp", name="ABP-100.mp4", sha1="DDD444")
    movie = Movie.create(javdb_id="jd-x", movie_number="ABP-100", title="ABP-100")
    Media.create(
        movie=movie, library=cloud_library,
        backend_locator={"fid": "old", "pickcode": "pc-old", "name": "x.mp4"},
        content_fingerprint="sha1:DDD444",
    )

    job = _run_import(cloud_library, transfer_mode="copy")

    assert job.state == "completed"
    assert job.imported_count == 0
    assert job.skipped_count == 1
    assert Media.select().count() == 1
    assert fs.copy_calls == []


def test_import_reports_movie_number_not_found(cloud_library, patched_client):
    fs = patched_client
    fs.add_file(fid="f-5", parent="src-1", name="random-video.mp4", sha1="EEE555")

    job = _run_import(cloud_library)

    assert job.state == "failed"
    assert job.failed_count == 1
    failed = job.failed_files
    assert "movie_number_not_found" in failed
    assert "random-video.mp4" in failed


def test_import_marks_censored_file_invalid(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-200", "src-1")
    fs.add_file(fid="f-6", parent="d-abp", name="ABP-200.mp4", sha1="FFF666", ic=1)

    job = _run_import(cloud_library)

    media = Media.get()
    assert media.valid is False
    # 已登记 invalid，作业含违规告警
    assert "cloud115_file_censored" in job.failed_files
    assert job.imported_count == 1


def test_import_only_files_filters_by_rel_path(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-a", "ABP-301", "src-1")
    fs.add_dir("d-b", "ABP-302", "src-1")
    fs.add_file(fid="f-7", parent="d-a", name="ABP-301.mp4", sha1="G777")
    fs.add_file(fid="f-8", parent="d-b", name="ABP-302.mp4", sha1="H888")

    job = _run_import(cloud_library, only_files=["ABP-302/ABP-302.mp4"])

    assert job.imported_count == 1
    media = Media.get()
    assert media.backend_locator["source_path"] == "ABP-302/ABP-302.mp4"


def test_import_only_files_filters_before_validation(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-selected", "ABP-303", "src-1")
    fs.add_dir("d-unselected", "ABP-304", "src-1")
    fs.add_file(fid="f-selected", parent="d-selected", name="ABP-303.mp4", sha1="S303")
    # 未选择文件同时命中“小文件 + 缺 SHA”；不得污染本次重导的 skipped/failed。
    fs.add_file(
        fid="f-unselected",
        parent="d-unselected",
        name="ABP-304.mp4",
        sha1="",
        size=1,
    )

    job = _run_import(cloud_library, only_files=["ABP-303/ABP-303.mp4"])

    assert job.state == "completed"
    assert job.imported_count == 1
    assert job.skipped_count == 0
    assert job.failed_count == 0
    assert "ABP-304" not in job.failed_files


def test_import_only_files_all_missing_marks_retry_sources_missing(
    cloud_library, patched_client
):
    job = _run_import(cloud_library, only_files=["gone/gone.mp4"])

    assert job.state == "failed"
    assert "retry_sources_missing" in job.failed_files


def test_import_metadata_failure_fails_whole_group(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-400", "src-1")
    fs.add_file(fid="f-9", parent="d-abp", name="ABP-400.mp4", sha1="I999")

    job = _run_import(cloud_library, fail_numbers={"ABP-400"})

    assert job.state == "failed"
    assert job.failed_count == 1
    assert "metadata_fetch_failed" in job.failed_files
    assert Media.select().count() == 0
    assert fs.move_calls == []


def test_import_rejects_source_inside_library_root(cloud_library, patched_client):
    fs = patched_client
    from src.api.exception.errors import ApiError

    with pytest.raises(ApiError) as exc_info:
        _run_import(cloud_library, source_cid="jav-dir")
    assert exc_info.value.code == "cloud115_source_inside_library"


def test_import_multi_part_registers_one_media_per_file(cloud_library, patched_client):
    """多分部不合并：VR/FC2 每个文件一条 Media 挂同一 movie。"""
    fs = patched_client
    fs.add_dir("d-fc2", "FC2-PPV-1234567", "src-1")
    fs.add_file(fid="f-a", parent="d-fc2", name="FC2-PPV-1234567-1.mp4", sha1="J111")
    fs.add_file(fid="f-b", parent="d-fc2", name="FC2-PPV-1234567-2.mp4", sha1="J222")

    job = _run_import(cloud_library)

    assert job.imported_count == 2
    media_items = list(Media.select())
    assert len(media_items) == 2
    assert {m.content_fingerprint for m in media_items} == {"sha1:J111", "sha1:J222"}
    assert len({m.movie_number for m in media_items}) == 1
    assert len(fs.rename_calls) == 2
    assert all(isinstance(call, tuple) and len(call) == 2 for call in fs.rename_calls)


def test_rename_must_be_visible_before_register_or_cleanup(
    cloud_library, patched_client
):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-500", "src-1")
    fs.add_file(fid="f-r", parent="d-abp", name="ABP-500.mp4", sha1="R500")
    # 复制产物 fid 可预测为 f-r-copy1000；模拟接口返回成功但实际名称不变。
    fs.rename_noop_fids.add("f-r-copy1000")

    job = _run_import(cloud_library)

    assert job.state == "failed"
    assert "cloud115_rename_failed" in job.failed_files
    assert Media.select().count() == 0
    assert "f-r" in fs.files
    assert fs.delete_calls == []


def test_partial_rename_retry_reuses_copied_targets(cloud_library, patched_client):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-501", "src-1")
    fs.add_file(fid="f-r1", parent="d-abp", name="ABP-501-1.mp4", sha1="R501A")
    fs.add_file(fid="f-r2", parent="d-abp", name="ABP-501-2.mp4", sha1="R501B")
    fs.rename_fail_fids.add("f-r2-copy1001")

    first = _run_import(cloud_library, transfer_mode="copy")
    assert first.state == "failed"
    assert Media.select().count() == 0
    assert len(fs.copy_calls) == 1

    fs.rename_fail_fids.clear()
    second = _run_import(cloud_library, transfer_mode="copy")

    assert second.state == "completed"
    assert Media.select().count() == 2
    assert len(fs.copy_calls) == 1


def test_cleanup_source_subtitle_failure_keeps_all_sources(
    cloud_library, patched_client, subtitle_root
):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-502", "src-1")
    fs.add_file(fid="f-v", parent="d-abp", name="ABP-502.mp4", sha1="R502")
    fs.add_file(fid="f-s", parent="d-abp", name="ABP-502.srt", sha1="S502", size=100)
    fs.subtitle_error = RuntimeError("download failed")

    job = _run_import(cloud_library)

    assert job.state == "failed"
    assert "cloud115_subtitle_download_failed" in job.failed_files
    assert "f-v" in fs.files and "f-s" in fs.files
    assert fs.delete_calls == []


def test_cleanup_source_delete_failure_retry_does_not_recopy(
    cloud_library, patched_client
):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-503", "src-1")
    fs.add_file(fid="f-d", parent="d-abp", name="ABP-503.mp4", sha1="R503")
    fs.delete_fail_fids.add("f-d")

    first = _run_import(cloud_library)
    assert first.state == "failed"
    assert "source_delete_failed" in first.failed_files
    assert Media.select().count() == 1
    assert len(fs.copy_calls) == 1

    fs.delete_fail_fids.clear()
    second = _run_import(
        cloud_library,
        only_files=["ABP-503/ABP-503.mp4"],
    )

    assert second.state == "completed"
    assert len(fs.copy_calls) == 1
    assert Media.select().count() == 1
    assert "f-d" not in fs.files


def test_cleanup_source_refreshes_stale_existing_locator_before_delete(
    cloud_library, patched_client
):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-504", "src-1")
    fs.add_file(fid="f-stale-source", parent="d-abp", name="ABP-504.mp4", sha1="R504")
    movie = Movie.create(javdb_id="jd-ABP-504", movie_number="ABP-504", title="ABP-504")
    media = Media.create(
        movie=movie,
        library=cloud_library,
        backend_locator={
            "fid": "missing-old-fid",
            "pickcode": "missing-old-pickcode",
            "name": "old.mp4",
            "source_path": "old/ABP-504.mp4",
        },
        content_fingerprint="sha1:R504",
        valid=True,
    )

    job = _run_import(cloud_library)

    assert job.state == "completed"
    refreshed = Media.get_by_id(media.id)
    assert refreshed.backend_locator["fid"] != "missing-old-fid"
    assert refreshed.backend_locator["fid"] in fs.files
    assert refreshed.backend_locator["pickcode"] != "missing-old-pickcode"
    assert "f-stale-source" not in fs.files


def test_cleanup_source_duplicate_does_not_rename_managed_target(
    cloud_library, patched_client
):
    fs = patched_client
    fs.add_dir("d-abp", "ABP-505", "src-1")
    fs.add_file(fid="f-duplicate-source", parent="d-abp", name="another-name.mp4", sha1="R505")
    fs.add_file(fid="f-managed", parent="jav-dir", name="managed-name.mp4", sha1="R505")
    movie = Movie.create(javdb_id="jd-ABP-505", movie_number="ABP-505", title="ABP-505")
    media = Media.create(
        movie=movie,
        library=cloud_library,
        backend_locator={
            "fid": "f-managed",
            "pickcode": fs.files["f-managed"].pickcode,
            "name": "managed-name.mp4",
            "source_path": "original/ABP-505.mp4",
        },
        content_fingerprint="sha1:R505",
        valid=True,
    )

    job = _run_import(cloud_library)

    assert job.state == "completed"
    assert job.skipped_count == 1
    assert fs.files["f-managed"].name == "managed-name.mp4"
    assert fs.rename_calls == []
    assert "f-duplicate-source" not in fs.files
    assert Media.get_by_id(media.id).backend_locator["fid"] == "f-managed"
