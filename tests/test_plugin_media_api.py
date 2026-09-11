"""插件媒体只读契约：按影片/媒体库读取媒体快照并批量判断存在性。"""

from dataclasses import FrozenInstanceError

import pytest

from src.model import Media, MediaLibrary, Movie
from src.plugins import PluginContext, PluginMediaPresence, PluginMediaSnapshot


def _movie(number: str) -> Movie:
    return Movie.create(
        javdb_id=f"javdb-{number}",
        movie_number=number,
        title=f"影片 {number}",
        summary="",
    )


def test_list_media_for_movie_exposes_library_and_technical_metadata(test_db, tmp_path):
    movie = _movie("ABC-001")
    primary = MediaLibrary.create(
        name="主媒体库",
        provider_key="local",
        provider_config={},
    )
    archive = MediaLibrary.create(
        name="归档媒体库",
        provider_key="cloud115",
        provider_config={},
    )
    first = Media.create(
        movie=movie,
        library=primary,
        file_name="ABC-001-1080p.mkv",
        resolution="1920x1080",
        file_size_bytes=1024,
        duration_seconds=3600,
        video_info={"codec": "hevc"},
    )
    second = Media.create(
        movie=movie,
        library=archive,
        file_name="ABC-001-720p.mp4",
        resolution="1280x720",
        file_size_bytes=512,
        valid=False,
    )

    api = PluginContext("media_demo", {}, tmp_path).media
    items = api.list_for_movie(movie.id)

    assert all(isinstance(item, PluginMediaSnapshot) for item in items)
    assert [item.media_id for item in items] == [first.id, second.id]
    assert items[0].library_id == primary.id
    assert items[0].library_name == "主媒体库"
    assert items[0].provider_key == "local"
    assert items[0].resolution == "1920x1080"
    assert items[0].file_size_bytes == 1024
    assert items[0].video_info == {"codec": "hevc"}
    assert items[1].valid is False

    filtered = api.list_for_movie(movie.id, library_id=primary.id)
    assert [item.media_id for item in filtered] == [first.id]
    with pytest.raises(FrozenInstanceError):
        items[0].file_name = "changed"
    assert Media.get_by_id(first.id).file_name == "ABC-001-1080p.mkv"


def test_presence_for_movies_distinguishes_any_and_playable_media_by_library(
    test_db,
    tmp_path,
):
    first = _movie("ABC-001")
    second = _movie("ABC-002")
    library = MediaLibrary.create(
        name="媒体库",
        provider_key="local",
        provider_config={},
    )
    other_library = MediaLibrary.create(
        name="另一个媒体库",
        provider_key="local",
        provider_config={},
    )
    Media.create(
        movie=first,
        library=library,
        file_name="invalid.mkv",
        valid=False,
    )
    Media.create(
        movie=first,
        library=other_library,
        file_name="playable.mkv",
        valid=True,
    )

    presence = PluginContext("media_demo", {}, tmp_path).media.presence_for_movies(
        [first.id, second.id, first.id],
        library_id=library.id,
    )

    assert set(presence) == {first.id, second.id}
    assert isinstance(presence[first.id], PluginMediaPresence)
    assert presence[first.id].has_any is True
    assert presence[first.id].has_playable is False
    assert [item.file_name for item in presence[first.id].items] == ["invalid.mkv"]
    assert presence[second.id].has_any is False
    assert presence[second.id].has_playable is False

    all_libraries = PluginContext("media_demo", {}, tmp_path).media.presence_for_movies(
        [first.id]
    )
    assert all_libraries[first.id].has_playable is True
    assert len(all_libraries[first.id].items) == 2
