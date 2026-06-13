import pytest

from src.model import (
    Image,
    Media,
    MediaLibrary,
    Movie,
    MovieSeries,
    VideoCollection,
    VideoCollectionItem,
    VideoItem,
)
from src.model.base import database_proxy
from src.schema.videos.imports import VideoImportRequest
from src.service.videos import VideoImportService

_MODELS = [
    Image,
    MovieSeries,
    Movie,
    VideoItem,
    VideoCollection,
    VideoCollectionItem,
    MediaLibrary,
    Media,
]


@pytest.fixture()
def video_import_tables(test_db):
    test_db.bind(_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(_MODELS)
    yield test_db
    test_db.drop_tables(list(reversed(_MODELS)))
    # 还原到全局 proxy，避免把模型永久绑定在已关闭的测试库上污染后续用例。
    for model in _MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)


def _make_video_file(directory, name: str) -> str:
    path = directory / name
    # 写入非视频内容即可：探测会优雅返回空结果，不影响登记。
    path.write_bytes(b"fake-video-bytes-" + name.encode())
    return str(path)


def test_import_directory_creates_video_items_and_media(video_import_tables, tmp_path):
    source = tmp_path / "clips"
    source.mkdir()
    _make_video_file(source, "alpha.mp4")
    _make_video_file(source, "beta.mkv")
    _make_video_file(source, "note.txt")  # 非视频后缀应被忽略

    collection = VideoCollection.create(name="合集")

    result = VideoImportService().import_from_source(
        VideoImportRequest(
            source_path=str(source),
            collection_id=collection.id,
        )
    )

    assert result.created_count == 2
    assert result.skipped_count == 0
    assert VideoItem.select().count() == 2
    assert Media.select().count() == 2

    # 每条 Media 归属 video_item 而非 movie。
    for media in Media.select():
        assert media.video_item_id is not None
        assert media.movie_number is None
        assert media.special_tags == "普通"
        assert media.content_fingerprint

    # 合集按入参关联到每个视频。
    items = list(VideoCollectionItem.select().order_by(VideoCollectionItem.position))
    assert [item.position for item in items] == [0, 1]

    # 标题默认取文件名 stem，按目录排序。
    titles = sorted(video.title for video in VideoItem.select())
    assert titles == ["alpha", "beta"]


def test_import_single_file_skips_already_indexed(video_import_tables, tmp_path):
    file_path = _make_video_file(tmp_path, "solo.mp4")
    service = VideoImportService()

    first = service.import_from_source(VideoImportRequest(source_path=file_path))
    assert first.created_count == 1

    # 路径唯一即去重，重复导入跳过。
    second = service.import_from_source(VideoImportRequest(source_path=file_path))
    assert second.created_count == 0
    assert second.skipped_count == 1
    assert VideoItem.select().count() == 1


def test_import_rejects_unsupported_file(video_import_tables, tmp_path):
    from src.api.exception.errors import ApiError

    file_path = _make_video_file(tmp_path, "doc.txt")
    with pytest.raises(ApiError) as exc:
        VideoImportService().import_from_source(VideoImportRequest(source_path=file_path))
    assert exc.value.code == "import_source_unsupported"


def test_import_dedupes_by_content_fingerprint(video_import_tables, tmp_path):
    # 相同内容、不同路径/文件名 → 指纹相同 → 第二次按内容指纹去重跳过。
    content = b"identical-video-content-payload-0123456789"
    first = tmp_path / "a.mp4"
    first.write_bytes(content)
    nested = tmp_path / "sub"
    nested.mkdir()
    second = nested / "b.mp4"
    second.write_bytes(content)

    service = VideoImportService()
    first_result = service.import_from_source(VideoImportRequest(source_path=str(first)))
    assert first_result.created_count == 1

    second_result = service.import_from_source(VideoImportRequest(source_path=str(second)))
    assert second_result.created_count == 0
    assert second_result.skipped_count == 1
    assert VideoItem.select().count() == 1
    assert Media.select().count() == 1
