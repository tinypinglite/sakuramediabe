import json
from datetime import datetime
from pathlib import Path

import pytest

from src.api.exception.errors import ApiError
from src.model import (
    BackgroundTaskRun,
    Image,
    Media,
    MediaLibrary,
    Movie,
    MovieSeries,
    ResourceTaskState,
    VideoCollection,
    VideoCollectionItem,
    VideoImportJob,
    VideoItem,
)
from src.model.base import database_proxy
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeResult
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.videos import VideoImportService
from src.service.videos import video_import_service as video_import_module

_MODELS = [
    Image,
    MovieSeries,
    Movie,
    VideoItem,
    VideoCollection,
    VideoCollectionItem,
    BackgroundTaskRun,
    VideoImportJob,
    MediaLibrary,
    Media,
    ResourceTaskState,
]


@pytest.fixture()
def video_import_tables(test_db):
    test_db.bind(_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(_MODELS)
    yield test_db
    for model in _MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)


@pytest.fixture(autouse=True)
def _stub_cover(monkeypatch):
    # 默认禁用真实首帧解码（测试用假视频字节），避免 PyAV 噪声；需要时单测自行覆盖。
    monkeypatch.setattr(
        video_import_module.VideoCoverService,
        "generate_cover",
        staticmethod(lambda video, path: None),
    )


def _make_library(tmp_path) -> MediaLibrary:
    root = tmp_path / "lib"
    root.mkdir()
    return MediaLibrary.create(name="videos-lib", backend="local", backend_config={"root_path": str(root)})


def _make_video_file(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"fake-video-bytes-" + name.encode())
    return path


def test_import_hardlinks_into_library_and_creates_media(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    _make_video_file(source, "alpha.mp4")
    _make_video_file(source, "beta.mkv")
    _make_video_file(source, "note.txt")  # 非视频后缀忽略

    job = VideoImportService().import_from_source(str(source), library.id)

    assert job.state == "completed"
    assert job.imported_count == 2
    assert job.skipped_count == 0
    assert job.failed_count == 0
    assert VideoItem.select().count() == 2
    assert Media.select().count() == 2

    library_root = Path(library.backend_config["root_path"])
    for media in Media.select():
        assert media.video_item_id is not None
        assert media.movie_number is None
        assert media.library_id == library.id
        target = Path(media.path)
        # 文件已搬入媒体库根目录，且默认 auto 模式不删源。
        assert library_root in target.parents
        assert target.exists()
        assert media.storage_mode in ("hardlink", "copy")
        # 缩略图任务被置为待处理，交给后台生成。
        state = ResourceTaskState.get_or_none(
            ResourceTaskState.task_key == MediaThumbnailService.TASK_KEY,
            ResourceTaskState.resource_id == media.id,
        )
        assert state is not None and state.state == "pending"

    # auto 模式源文件保留。
    assert (source / "alpha.mp4").exists()


def test_cleanup_source_copies_and_deletes_source(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "drop"
    source.mkdir()
    src_file = _make_video_file(source, "clip.mp4")

    job = VideoImportService().import_from_source(
        str(source), library.id, transfer_mode="cleanup-source"
    )

    assert job.state == "completed"
    assert job.imported_count == 1
    media = Media.get()
    assert media.storage_mode == "copy"
    assert Path(media.path).exists()
    # cleanup-source 删除源文件。
    assert not src_file.exists()


def test_library_required(video_import_tables, tmp_path):
    file_path = _make_video_file(tmp_path, "solo.mp4")
    with pytest.raises(ApiError) as exc:
        VideoImportService().import_from_source(str(file_path), None)
    assert exc.value.code == "media_library_required"


def test_cleanup_source_inside_library_rejected(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    inside = Path(library.backend_config["root_path"]) / "incoming"
    inside.mkdir()
    _make_video_file(inside, "x.mp4")
    with pytest.raises(ApiError) as exc:
        VideoImportService().import_from_source(
            str(inside), library.id, transfer_mode="cleanup-source"
        )
    assert exc.value.code == "cleanup_source_inside_media_library"


def test_import_skips_already_indexed_and_duplicate_fingerprint(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    file_path = _make_video_file(source, "solo.mp4")
    service = VideoImportService()

    first = service.import_from_source(str(file_path), library.id)
    assert first.imported_count == 1

    # 相同内容、不同路径 → 指纹去重跳过。
    nested = source / "again"
    nested.mkdir()
    dup = nested / "copy.mp4"
    dup.write_bytes(file_path.read_bytes())
    second = service.import_from_source(str(source), library.id)
    assert second.imported_count == 0
    # Media.path 记录的是搬入库后的目标路径，源路径并未登记，故两次跳过都走内容指纹去重。
    assert second.skipped_count == 2
    assert VideoItem.select().count() == 1
    # 跳过项写入 failed_files（kind=skipped），但不污染失败数、不让任务变红。
    assert second.failed_count == 0
    assert second.state == "completed"
    skipped_entries = [item for item in json.loads(second.failed_files) if item["kind"] == "skipped"]
    assert len(skipped_entries) == 2
    assert {item["reason"] for item in skipped_entries} == {"duplicate_fingerprint"}


def test_import_associates_collection(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    collection = VideoCollection.create(name="合集")
    source = tmp_path / "src"
    source.mkdir()
    _make_video_file(source, "a.mp4")
    _make_video_file(source, "b.mp4")

    job = VideoImportService().import_from_source(
        str(source), library.id, collection_id=collection.id
    )
    assert job.imported_count == 2
    positions = [item.position for item in VideoCollectionItem.select().order_by(VideoCollectionItem.position)]
    assert positions == [0, 1]


def test_cover_generation_invoked_per_file(video_import_tables, tmp_path, monkeypatch):
    library = _make_library(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    _make_video_file(source, "a.mp4")
    _make_video_file(source, "b.mp4")

    calls = []
    monkeypatch.setattr(
        video_import_module.VideoCoverService,
        "generate_cover",
        staticmethod(lambda video, path: calls.append((video.id, Path(path).name))),
    )

    job = VideoImportService().import_from_source(str(source), library.id)
    assert job.imported_count == 2
    # 每个成功导入的视频都触发一次首帧封面生成。
    assert sorted(name for _, name in calls) == ["a.mp4", "b.mp4"]


def test_collection_failure_rolls_back_file_and_keeps_source(video_import_tables, tmp_path, monkeypatch):
    # 集合关联与 Media 同事务：add_item 失败应整文件回滚（不留 Media/VideoItem），
    # 且 cleanup-source 删源在其后，源文件必须保留。
    library = _make_library(tmp_path)
    collection = VideoCollection.create(name="合集")
    source = tmp_path / "src"
    source.mkdir()
    src_file = _make_video_file(source, "clip.mp4")

    def _boom(collection_id, video_item_id):
        raise RuntimeError("collection add failed")

    monkeypatch.setattr(
        video_import_module.VideoCollectionService, "add_item", staticmethod(_boom)
    )

    job = VideoImportService().import_from_source(
        str(source), library.id, transfer_mode="cleanup-source", collection_id=collection.id
    )

    assert job.imported_count == 0
    assert job.failed_count == 1
    assert job.state == "failed"
    # 整文件回滚：无残留 Media / VideoItem。
    assert Media.select().count() == 0
    assert VideoItem.select().count() == 0
    # 删源在集合关联之后，回滚路径不会删除源文件。
    assert src_file.exists()


def test_unsupported_single_file_marks_job_failed(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    file_path = _make_video_file(tmp_path, "doc.txt")
    with pytest.raises(ApiError) as exc:
        VideoImportService().import_from_source(str(file_path), library.id)
    assert exc.value.code == "import_source_unsupported"
    # 作业已落库并标记失败。
    job = VideoImportJob.get()
    assert job.state == "failed"


class _FixedProbe:
    """注入用 fake probe：固定返回指定 creation_time，其余字段留空。"""

    def __init__(self, creation_time):
        self._creation_time = creation_time

    def probe_file(self, _file_path: Path) -> MediaMetadataProbeResult:
        return MediaMetadataProbeResult(creation_time=self._creation_time)


def test_import_writes_release_date_from_container_creation_time(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    _make_video_file(source, "alpha.mp4")

    creation_time = datetime(2023, 1, 15, 10, 30)
    service = VideoImportService(media_metadata_probe_service=_FixedProbe(creation_time))
    job = service.import_from_source(str(source), library.id)

    assert job.imported_count == 1
    # 容器 creation_time 被写入 release_date（UTC naive）。
    assert VideoItem.get().release_date == creation_time


def test_import_leaves_release_date_empty_when_no_creation_time(video_import_tables, tmp_path):
    library = _make_library(tmp_path)
    source = tmp_path / "src"
    source.mkdir()
    _make_video_file(source, "alpha.mp4")

    service = VideoImportService(media_metadata_probe_service=_FixedProbe(None))
    job = service.import_from_source(str(source), library.id)

    assert job.imported_count == 1
    # 读不到 creation_time 时如实留空，不做 mtime 兜底。
    assert VideoItem.get().release_date is None
