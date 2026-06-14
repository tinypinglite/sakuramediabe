"""VideoCoverService.generate_cover 的封面落库兜底测试。

封面是增益项：解码或 DB 写失败都必须只记日志返回 None，绝不外抛，
否则已成功入库的视频会被导入主流程误判为失败文件。
"""

from pathlib import Path

import pytest
from peewee import IntegrityError

from src.model import Image, VideoItem
from src.model.base import database_proxy
from src.service.videos import video_cover_service as video_cover_module
from src.service.videos.video_cover_service import VideoCoverService

_MODELS = [Image, VideoItem]


@pytest.fixture()
def cover_tables(test_db):
    test_db.bind(_MODELS, bind_refs=False, bind_backrefs=False)
    test_db.create_tables(_MODELS)
    yield test_db
    test_db.drop_tables(list(reversed(_MODELS)))
    for model in _MODELS:
        model.bind(database_proxy, bind_refs=False, bind_backrefs=False)


class _FakeImage:
    def save(self, path, format, quality):  # noqa: A002 - 对齐 PIL.Image.save 签名
        Path(path).write_bytes(b"webp-bytes")


class _FakeFrame:
    def to_image(self):
        return _FakeImage()


class _FakeStreams:
    def __init__(self):
        # 非空即可，generate_cover 只判 `container.streams.video` 真值并取首个。
        self.video = [object()]


class _FakeContainer:
    def __init__(self):
        self.streams = _FakeStreams()

    def decode(self, stream):
        yield _FakeFrame()

    def close(self):
        return None


class _FakeAv:
    def open(self, path):
        return _FakeContainer()


@pytest.fixture()
def _stub_av_and_root(tmp_path, monkeypatch):
    # 用假 av 让解码链路走通（写出 webp），并把图片根目录指向临时目录。
    monkeypatch.setattr(video_cover_module, "av", _FakeAv())
    monkeypatch.setattr(
        "src.service.playback.media_thumbnail_service.MediaThumbnailService._image_root_path",
        classmethod(lambda cls: tmp_path),
    )
    return tmp_path


def test_generate_cover_persists_image(cover_tables, _stub_av_and_root):
    tmp_path = _stub_av_and_root
    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"video")
    video = VideoItem.create(title="片")

    image = VideoCoverService.generate_cover(video, video_file)

    assert image is not None
    assert VideoItem.get_by_id(video.id).cover_image_id == image.id
    cover_file = tmp_path / "videos" / str(video.id) / "cover" / "0.webp"
    assert cover_file.exists()


def test_generate_cover_returns_none_when_db_write_fails(cover_tables, _stub_av_and_root, monkeypatch):
    tmp_path = _stub_av_and_root

    def _boom(*args, **kwargs):
        raise IntegrityError("db write failed")

    # 解码成功但建 Image 行失败：必须被吞掉，返回 None、不抛、不污染 cover_image。
    monkeypatch.setattr(video_cover_module.Image, "create", staticmethod(_boom))
    video_file = tmp_path / "v.mp4"
    video_file.write_bytes(b"video")
    video = VideoItem.create(title="片")

    result = VideoCoverService.generate_cover(video, video_file)

    assert result is None
    assert VideoItem.get_by_id(video.id).cover_image_id is None
