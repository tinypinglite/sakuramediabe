"""Cloud115HlsProxyService 单测：合播布局构建校验、DISCONTINUITY 边界、playlist 渲染。

不触达真实 115：resolve_media_segments 打桩返回假分段，聚焦布局/渲染/校验逻辑。
"""

import uuid

import pytest

from src.model import Media, MediaLibrary, Movie
from src.model.enums import MediaLibraryBackend
from src.service.playback.cloud115_hls_proxy_service import (
    Cloud115HlsProxyService,
    HlsSegment,
    MergedHlsLayout,
)


@pytest.fixture(autouse=True)
def _clear_proxy_caches(test_db):
    Cloud115HlsProxyService._layout_cache.clear()
    Cloud115HlsProxyService._segments_cache.clear()
    yield
    Cloud115HlsProxyService._layout_cache.clear()
    Cloud115HlsProxyService._segments_cache.clear()


def _unique() -> str:
    return uuid.uuid4().hex[:8]


def _cloud_library() -> MediaLibrary:
    return MediaLibrary.create(
        name=f"test-115-{_unique()}",
        backend=MediaLibraryBackend.CLOUD115.value,
        backend_config={"cookies": "UID=1", "root_cid": "0"},
    )


def _local_library() -> MediaLibrary:
    return MediaLibrary.create(
        name=f"test-local-{_unique()}",
        backend="local",
        backend_config={"root_path": "/library"},
    )


def _movie(movie_number: str) -> Movie:
    movie, _ = Movie.get_or_create(
        movie_number=movie_number,
        defaults={"javdb_id": f"javdb-{movie_number}", "title": f"title-{movie_number}"},
    )
    return movie


def _media(movie_number: str, library: MediaLibrary, *, valid: bool = True) -> Media:
    _movie(movie_number)
    return Media.create(
        movie=movie_number,
        library=library,
        path=f"/library/{movie_number}-{_unique()}.mp4",
        backend_locator={"pickcode": f"pc-{_unique()}", "fid": f"fid-{_unique()}"},
        valid=valid,
    )


def _fake_segments(media_id: int, count: int, prefix: str) -> tuple[HlsSegment, ...]:
    return tuple(
        HlsSegment(
            media_id=media_id,
            local_index=i,
            duration_seconds=10.0,
            url=f"https://cdn.example/{prefix}/{i}.ts",
        )
        for i in range(count)
    )


class TestBuildMergedLayout:
    async def test_builds_layout_with_discontinuity_boundaries(self, monkeypatch):
        lib = _cloud_library()
        m1 = _media("ABC-001", lib)
        m2 = _media("ABC-001", lib)

        async def _resolve(media, ua):
            return _fake_segments(media.id, 2 if media.id == m1.id else 3, media.id)

        monkeypatch.setattr(Cloud115HlsProxyService, "resolve_media_segments", _resolve)

        layout = await Cloud115HlsProxyService.build_merged_layout([m1.id, m2.id], "ua")
        assert [s.media_id for s in layout.segments] == [m1.id, m1.id, m2.id, m2.id, m2.id]
        assert layout.discontinuity_indexes == frozenset({2})
        assert layout.total_duration == 50.0
        assert layout.target_duration == 11
        assert layout.media_ids == (m1.id, m2.id)

    async def test_single_media_no_discontinuity(self, monkeypatch):
        lib = _cloud_library()
        m1 = _media("ABC-002", lib)

        async def _resolve(media, ua):
            return _fake_segments(media.id, 3, media.id)

        monkeypatch.setattr(Cloud115HlsProxyService, "resolve_media_segments", _resolve)

        layout = await Cloud115HlsProxyService.build_merged_layout([m1.id], "ua")
        assert layout.discontinuity_indexes == frozenset()

    async def test_rejects_local_media(self, monkeypatch):
        lib = _cloud_library()
        local_lib = _local_library()
        m_cloud = _media("ABC-003", lib)
        m_local = _media("ABC-003", local_lib)
        monkeypatch.setattr(
            Cloud115HlsProxyService, "resolve_media_segments", lambda media, ua: ()
        )

        with pytest.raises(Exception) as exc_info:
            await Cloud115HlsProxyService.build_merged_layout([m_cloud.id, m_local.id], "ua")
        assert exc_info.value.code == "merged_hls_not_cloud115"

    async def test_rejects_cross_movie(self, monkeypatch):
        lib = _cloud_library()
        m1 = _media("ABC-004", lib)
        m2 = _media("ABC-005", lib)
        monkeypatch.setattr(
            Cloud115HlsProxyService, "resolve_media_segments", lambda media, ua: ()
        )

        with pytest.raises(Exception) as exc_info:
            await Cloud115HlsProxyService.build_merged_layout([m1.id, m2.id], "ua")
        assert exc_info.value.code == "merged_hls_cross_movie"

    async def test_rejects_missing_media(self, monkeypatch):
        with pytest.raises(Exception) as exc_info:
            await Cloud115HlsProxyService.build_merged_layout([999999], "ua")
        assert exc_info.value.code == "media_not_found"

    async def test_rejects_empty(self):
        with pytest.raises(Exception) as exc_info:
            await Cloud115HlsProxyService.build_merged_layout([], "ua")
        assert exc_info.value.code == "merged_hls_need_at_least_one"


class TestRenderPlaylist:
    def test_discontinuity_placed_at_boundary(self):
        layout = MergedHlsLayout(
            media_ids=(1, 2),
            segments=(
                HlsSegment(1, 0, 10.0, "u1"),
                HlsSegment(1, 1, 10.0, "u2"),
                HlsSegment(2, 0, 10.0, "u3"),
                HlsSegment(2, 1, 10.0, "u4"),
            ),
            discontinuity_indexes=frozenset({2}),
            total_duration=40.0,
            target_duration=11,
        )
        playlist = Cloud115HlsProxyService.render_playlist(
            layout,
            media_ids_param="1,2",
            expires=123,
            signature="sig",
        )
        lines = playlist.splitlines()
        assert lines[0] == "#EXTM3U"
        assert "#EXT-X-TARGETDURATION:11" in lines
        # DISCONTINUITY 只出现在 index 2 之前，共 1 处。
        assert lines.count("#EXT-X-DISCONTINUITY") == 1
        disc_index = lines.index("#EXT-X-DISCONTINUITY")
        assert lines[disc_index + 1] == "#EXTINF:10.000,"
        assert lines[disc_index + 2] == "/media/hls-segment/2.ts?media_ids=1,2&expires=123&signature=sig"
        assert playlist.endswith("#EXT-X-ENDLIST\n")

    def test_single_playlist_no_discontinuity(self):
        layout = MergedHlsLayout(
            media_ids=(7,),
            segments=(HlsSegment(7, 0, 10.0, "u1"), HlsSegment(7, 1, 10.0, "u2")),
            discontinuity_indexes=frozenset(),
            total_duration=20.0,
            target_duration=11,
        )
        playlist = Cloud115HlsProxyService.render_playlist(
            layout, media_ids_param="7", expires=1, signature="s"
        )
        assert "#EXT-X-DISCONTINUITY" not in playlist
        assert "/media/hls-segment/1.ts?media_ids=7&expires=1&signature=s" in playlist
