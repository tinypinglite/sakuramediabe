"""合并播放流端点（/media/{media_id}/merged-stream）API 测试。

验证：签名鉴权、全量与 Range(206) 响应字节与手工拼接一致、不足 2 段/云端媒体返回 422。
"""

import hashlib
import hmac
import shutil
import struct
import subprocess

import pytest

from src.config.config import settings
from src.model import Media, MediaLibrary, Movie
from src.model.enums import MediaLibraryBackend

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg"),
    reason="需要 ffmpeg 生成测试 mp4",
)

from tests.conftest import TEST_FILE_SIGNATURE_EXPIRES, TEST_FILE_SIGNATURE_SECRET


def _make_part(tmp_path, name, duration):
    out = tmp_path / f"{name}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "faststart", "-y", str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def _signed_merged_url(
    media_ids: list[int], expires: int = TEST_FILE_SIGNATURE_EXPIRES
) -> str:
    signature = hmac.new(
        TEST_FILE_SIGNATURE_SECRET.encode("utf-8"),
        f"media:{media_ids[0]}:{expires}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    ids = ",".join(str(i) for i in media_ids)
    return (
        f"/media/merged-stream?media_ids={ids}"
        f"&expires={expires}&signature={signature}"
    )


def _hand_built(paths):
    # 用合并逻辑同款算法手工拼逻辑文件（仅测试内复制实现，验证端点字节一致）
    from src.service.playback import merged_mp4 as mm

    parts = [mm.parse_file(str(p)) for p in paths]
    layout = mm.build_merged_layout(parts)
    data = bytearray()
    for kind, arg, off, ln in layout.resolve_range(0, layout.total_size):
        if kind == "mem":
            data += arg
        else:
            with open(arg, "rb") as f:
                f.seek(off)
                data += f.read(ln)
    return bytes(data)


class TestMergedPlaybackApi:
    def test_merged_stream_full_body(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        movie = Movie.create(javdb_id="javdb-1", movie_number="ABC-001", title="t")
        m1 = Media.create(movie=movie, path=str(p1), storage_mode="local", valid=True)
        m2 = Media.create(movie=movie, path=str(p2), storage_mode="local", valid=True)

        response = client.get(_signed_merged_url([m1.id, m2.id]))
        assert response.status_code == 200
        assert response.headers["accept-ranges"] == "bytes"
        expected = _hand_built([p1, p2])
        assert response.headers["content-length"] == str(len(expected))
        assert response.content == expected

    def test_merged_stream_range(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        movie = Movie.create(javdb_id="javdb-2", movie_number="ABC-002", title="t")
        m1 = Media.create(movie=movie, path=str(p1), storage_mode="local", valid=True)
        m2 = Media.create(movie=movie, path=str(p2), storage_mode="local", valid=True)
        expected = _hand_built([p1, p2])

        response = client.get(
            _signed_merged_url([m1.id, m2.id]), headers={"Range": "bytes=100-299"}
        )
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 100-299/{len(expected)}"
        assert response.content == expected[100:300]

    def test_merged_stream_tail_range(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        movie = Movie.create(javdb_id="javdb-3", movie_number="ABC-003", title="t")
        m1 = Media.create(movie=movie, path=str(p1), storage_mode="local", valid=True)
        m2 = Media.create(movie=movie, path=str(p2), storage_mode="local", valid=True)
        expected = _hand_built([p1, p2])

        response = client.get(
            _signed_merged_url([m1.id, m2.id]), headers={"Range": f"bytes=-500"}
        )
        assert response.status_code == 206
        assert response.content == expected[-500:]

    def test_merged_stream_rejects_single_segment(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        movie = Movie.create(javdb_id="javdb-4", movie_number="ABC-004", title="t")
        m1 = Media.create(movie=movie, path=str(p1), storage_mode="local", valid=True)

        response = client.get(_signed_merged_url([m1.id]))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "merged_mp4_need_at_least_two"

    def test_merged_stream_rejects_missing_segment(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        movie = Movie.create(javdb_id="javdb-6", movie_number="ABC-006", title="t")
        m1 = Media.create(movie=movie, path=str(p1), storage_mode="local", valid=True)
        Media.create(movie=movie, path=str(p2), storage_mode="local", valid=True)

        response = client.get(_signed_merged_url([m1.id, 99999]))
        assert response.status_code == 404

    def test_merged_stream_rejects_cross_movie(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        movie_a = Movie.create(javdb_id="javdb-7", movie_number="ABC-007", title="t")
        movie_b = Movie.create(javdb_id="javdb-8", movie_number="ABC-008", title="t")
        m1 = Media.create(movie=movie_a, path=str(p1), storage_mode="local", valid=True)
        m2 = Media.create(movie=movie_b, path=str(p2), storage_mode="local", valid=True)

        response = client.get(_signed_merged_url([m1.id, m2.id]))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "merged_mp4_cross_movie"

    def test_merged_stream_rejects_cloud115(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        movie = Movie.create(javdb_id="javdb-5", movie_number="ABC-005", title="t")
        cloud_lib = MediaLibrary.create(
            name="115-lib", backend=MediaLibraryBackend.CLOUD115.value
        )
        local_media = Media.create(
            movie=movie, path=str(p1), storage_mode="local", valid=True
        )
        cloud_media = Media.create(
            movie=movie,
            library=cloud_lib,
            backend_locator={"fid": "2", "pickcode": "y", "name": "b"},
            storage_mode="cloud",
            valid=True,
        )

        # 云端分段 → 422
        response = client.get(_signed_merged_url([cloud_media.id, local_media.id]))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "merged_mp4_unsupported"

    def test_merged_stream_follows_requested_order(self, client, test_db, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        movie = Movie.create(javdb_id="javdb-9", movie_number="ABC-009", title="t")
        m1 = Media.create(movie=movie, path=str(p1), storage_mode="local", valid=True)
        m2 = Media.create(movie=movie, path=str(p2), storage_mode="local", valid=True)

        # 逆序请求：p2 在前。签名锚点 = media_ids 第一个 = m2。
        response = client.get(_signed_merged_url([m2.id, m1.id]))
        assert response.status_code == 200
        expected = _hand_built([p2, p1])
        assert response.headers["content-length"] == str(len(expected))
        assert response.content == expected

    def test_merged_stream_second_request_reuses_cache(
        self, client, test_db, tmp_path, monkeypatch
    ):
        from src.service.playback import merged_playback_service as mps

        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        movie = Movie.create(javdb_id="javdb-10", movie_number="ABC-010", title="t")
        m1 = Media.create(movie=movie, path=str(p1), storage_mode="local", valid=True)
        m2 = Media.create(movie=movie, path=str(p2), storage_mode="local", valid=True)

        calls = {"n": 0}
        original = mps.build_merged_layout

        def counting(parts):
            calls["n"] += 1
            return original(parts)

        monkeypatch.setattr(mps, "build_merged_layout", counting)
        url = _signed_merged_url([m1.id, m2.id])

        # 首次请求触发构建；同 key 第二次请求应命中缓存，不再重复构建。
        assert client.get(url).status_code == 200
        assert calls["n"] == 1
        assert client.get(url).status_code == 200
        assert calls["n"] == 1
