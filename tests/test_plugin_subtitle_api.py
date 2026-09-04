"""公开字幕读取契约：真实 PostgreSQL 归属查询与临时文件访问。"""

import hashlib
import stat
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.common.media_paths import movie_subtitle_dir
from src.config.config import settings
from src.model import Movie, Subtitle
from src.plugins import PluginContext
from src.plugins.types import SubtitleAsset, SubtitleContent, SubtitleReadError
from src.service.catalog import movie_subtitle_service


@pytest.fixture
def library(test_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.media, "import_image_root_path", str(tmp_path / "assets"))
    movie = Movie.create(javdb_id="one", movie_number="ABC-001", title="影片", summary="")
    other = Movie.create(javdb_id="two", movie_number="ABC-002", title="其他影片")
    root = movie_subtitle_dir(movie.movie_number)
    root.mkdir(parents=True)
    context = PluginContext("subtitle_demo", {}, tmp_path / "plugin-data")
    return context, movie, other, root


def add_subtitle(movie, path, content=b"subtitle"):
    path.write_bytes(content)
    return Subtitle.create(movie=movie, file_path=str(path))


def test_list_only_registered_accessible_assets_is_readonly(library):
    context, movie, other, root = library
    first = add_subtitle(movie, root / "one.srt", b"first")
    second = add_subtitle(movie, root / "two.ASS", b"second")
    add_subtitle(other, root / "other.srt")
    (root / "unregistered.vtt").write_bytes(b"not registered")
    Subtitle.create(movie=movie, file_path=str(root / "missing.srt"))
    folder = root / "directory.srt"
    folder.mkdir()
    Subtitle.create(movie=movie, file_path=str(folder))
    outside = root.parent / "outside.srt"
    add_subtitle(movie, outside)
    add_subtitle(movie, root / "invalid.txt")
    before = list(Subtitle.select().order_by(Subtitle.id).dicts())

    items = context.subtitles.list(movie.id)

    assert isinstance(items, tuple)
    assert [item.subtitle_id for item in items] == [second.id, first.id]
    assert items[0] == SubtitleAsset(second.id, "two.ASS", "ass", 6, second.created_at)
    assert set(asdict(items[0])) == {
        "subtitle_id", "file_name", "format", "size_bytes", "created_at"
    }
    with pytest.raises(FrozenInstanceError):
        items[0].file_name = "changed"
    assert list(Subtitle.select().order_by(Subtitle.id).dicts()) == before
    assert outside.read_bytes() == b"subtitle"
    assert not context._data_dir.exists()


@pytest.mark.parametrize("suffix", ["srt", "ass", "ssa", "vtt"])
def test_read_returns_original_bytes_and_current_hash(library, suffix):
    context, movie, _, root = library
    content = "1\r\n00:00:01,000 --> 00:00:02,000\r\n字幕\r\n".encode("utf-16")
    path = root / f"one.{suffix}"
    row = add_subtitle(movie, path, content)

    result = context.subtitles.read(movie.id, row.id)

    assert result == SubtitleContent(row.id, content, hashlib.sha256(content).hexdigest())
    assert set(asdict(result)) == {"subtitle_id", "content", "sha256"}
    with pytest.raises(FrozenInstanceError):
        result.content = b"changed"
    assert path.read_bytes() == content
    path.write_bytes(b"replacement")
    fresh = context.subtitles.read(movie.id, row.id)
    assert fresh.content == b"replacement"
    assert fresh.sha256 == hashlib.sha256(fresh.content).hexdigest()
    assert fresh.sha256 != result.sha256


def test_missing_movie_and_empty_library(library):
    context, movie, _, _ = library
    assert context.subtitles.list(movie.id) == ()
    for action in (lambda: context.subtitles.list(-1), lambda: context.subtitles.read(-1, 1)):
        with pytest.raises(SubtitleReadError) as caught:
            action()
        assert caught.value.code == "movie_not_found"


def test_read_rejects_missing_or_other_movie_subtitle(library):
    context, movie, other, root = library
    row = add_subtitle(other, root / "other.srt")
    for subtitle_id in (row.id, -1):
        with pytest.raises(SubtitleReadError) as caught:
            context.subtitles.read(movie.id, subtitle_id)
        assert caught.value.code == "subtitle_not_found"


@pytest.mark.parametrize("kind", ["outside", "symlink", "extension", "symlink_loop"])
def test_invalid_paths_are_hidden_and_cannot_be_read(library, kind):
    context, movie, _, root = library
    path = root / "invalid.srt"
    if kind == "outside":
        path = root.parent / "outside.srt"
        path.write_bytes(b"private")
    elif kind == "symlink":
        outside = root.parent / "outside.srt"
        outside.write_bytes(b"private")
        path.symlink_to(outside)
    elif kind == "extension":
        path = root / "invalid.txt"
        path.write_bytes(b"private")
    else:
        path.symlink_to(path)
    row = Subtitle.create(movie=movie, file_path=str(path))

    assert context.subtitles.list(movie.id) == ()
    with pytest.raises(SubtitleReadError) as caught:
        context.subtitles.read(movie.id, row.id)
    assert caught.value.code == "subtitle_path_invalid"
    assert str(root) not in str(caught.value)
    assert Subtitle.get_by_id(row.id).file_path == str(path)


@pytest.mark.parametrize("kind", ["removed", "directory", "unreadable"])
def test_unavailable_file_returns_public_error(library, monkeypatch, kind):
    context, movie, _, root = library
    path = root / "one.srt"
    row = add_subtitle(movie, path)
    assert context.subtitles.list(movie.id)
    if kind == "unreadable":
        original_open = Path.open

        def denied(target, *args, **kwargs):
            if target == path:
                raise PermissionError(str(path))
            return original_open(target, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    else:
        path.unlink()
        if kind == "directory":
            path.mkdir()
        assert context.subtitles.list(movie.id) == ()
    with pytest.raises(SubtitleReadError) as caught:
        context.subtitles.read(movie.id, row.id)
    assert caught.value.code == "subtitle_unavailable"
    assert str(path) not in str(caught.value)


@pytest.mark.parametrize("size", [0, 8, 9])
def test_read_size_limit_including_exact_boundary(library, monkeypatch, size):
    context, movie, _, root = library
    monkeypatch.setattr(movie_subtitle_service, "MAX_SUBTITLE_CONTENT_BYTES", 8)
    row = add_subtitle(movie, root / "one.srt", b"x" * size)
    assert context.subtitles.list(movie.id)[0].size_bytes == size
    if size <= 8:
        assert context.subtitles.read(movie.id, row.id).content == b"x" * size
    else:
        with pytest.raises(SubtitleReadError) as caught:
            context.subtitles.read(movie.id, row.id)
        assert caught.value.code == "subtitle_too_large"


def test_read_bounds_content_even_when_file_grows(library, monkeypatch):
    context, movie, _, root = library
    monkeypatch.setattr(movie_subtitle_service, "MAX_SUBTITLE_CONTENT_BYTES", 8)
    row = add_subtitle(movie, root / "one.srt", b"x" * 100)
    # 模拟 stat 后内容变长；不能只依赖预先取得的文件大小。
    monkeypatch.setattr(movie_subtitle_service.os, "fstat", lambda fd: SimpleNamespace(
        st_mode=stat.S_IFREG, st_size=1,
    ))
    with pytest.raises(SubtitleReadError) as caught:
        context.subtitles.read(movie.id, row.id)
    assert caught.value.code == "subtitle_too_large"
