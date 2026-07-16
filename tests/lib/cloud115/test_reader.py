"""Cloud115RangeReader 单元测试：顺序 Range、seek/缓冲、UA、错误映射、请求受控性。"""

from __future__ import annotations

import httpx
import pytest

from src.lib.cloud115 import Cloud115RangeReader
from src.lib.cloud115.exceptions import Cloud115RequestError

FILE_CONTENT = bytes(range(256)) * 40  # 10240 字节
UA = "SakuraMedia-Thumbnail-Test/1.0"


def _make_reader(
    *,
    chunk_size: int = 1024,
    fail_status: int | None = None,
    max_fetched_bytes: int | None = None,
):
    requests_seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_status is not None:
            return httpx.Response(fail_status, content=b"denied")
        assert request.headers["User-Agent"] == UA
        range_header = request.headers["Range"]
        requests_seen.append((range_header, request.headers["User-Agent"]))
        start_s, end_s = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_s), int(end_s)
        return httpx.Response(
            206,
            content=FILE_CONTENT[start:end + 1],
            headers={"Content-Range": f"bytes {start}-{end}/{len(FILE_CONTENT)}"},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    reader = Cloud115RangeReader(
        "https://cdn.115.example/video.mp4?t=1",
        user_agent=UA,
        file_size=len(FILE_CONTENT),
        chunk_size=chunk_size,
        max_fetched_bytes=max_fetched_bytes,
        http_client=client,
    )
    return reader, requests_seen


def test_full_read_fetches_in_single_request():
    reader, seen = _make_reader(chunk_size=4096)
    data = reader.read()
    assert data == FILE_CONTENT
    # want 大于 chunk 时一次拉够，避免碎请求
    assert len(seen) == 1
    reader.close()


def test_chunked_sequential_reads_stay_serial():
    reader, seen = _make_reader(chunk_size=4096)
    data = b"".join(reader.read(4096) for _ in range(3))
    assert data == FILE_CONTENT
    # 每次恰好消费一个缓冲块：3 个请求、严格顺序、一次一个在途
    assert len(seen) == 3
    assert [r for r, _ in seen] == ["bytes=0-4095", "bytes=4096-8191", "bytes=8192-10239"]
    reader.close()


def test_small_reads_hit_buffer_without_new_requests():
    reader, seen = _make_reader(chunk_size=4096)
    first = reader.read(100)
    second = reader.read(100)
    assert first == FILE_CONTENT[:100]
    assert second == FILE_CONTENT[100:200]
    assert len(seen) == 1  # 两次小读命中同一个 4KB 缓冲
    reader.close()


def test_seek_and_read_tail():
    reader, seen = _make_reader(chunk_size=1024)
    reader.seek(-100, 2)
    assert reader.tell() == len(FILE_CONTENT) - 100
    tail = reader.read(100)
    assert tail == FILE_CONTENT[-100:]
    # seek 本身不发请求，read 才拉
    assert len(seen) == 1
    reader.close()


def test_seek_whence_variants():
    reader, _ = _make_reader()
    assert reader.seek(10) == 10
    assert reader.seek(5, 1) == 15
    assert reader.seek(0, 2) == len(FILE_CONTENT)
    assert reader.read(10) == b""  # 越界读返回空
    reader.close()


def test_read_past_eof_truncates():
    reader, _ = _make_reader(chunk_size=4096)
    reader.seek(len(FILE_CONTENT) - 10)
    assert reader.read(100) == FILE_CONTENT[-10:]
    reader.close()


def test_forbidden_status_raises_request_error():
    reader, _ = _make_reader(fail_status=403)
    with pytest.raises(Cloud115RequestError, match="403"):
        reader.read(10)
    reader.close()


def test_reader_validates_constructor_args():
    with pytest.raises(ValueError, match="url"):
        Cloud115RangeReader("", user_agent=UA, file_size=10)
    with pytest.raises(ValueError, match="user_agent"):
        Cloud115RangeReader("https://x", user_agent="", file_size=10)
    with pytest.raises(ValueError, match="file_size"):
        Cloud115RangeReader("https://x", user_agent=UA, file_size=0)
    with pytest.raises(ValueError, match="max_fetched_bytes"):
        Cloud115RangeReader(
            "https://x", user_agent=UA, file_size=10, max_fetched_bytes=0
        )


def test_reader_enforces_cumulative_fetch_budget():
    reader, seen = _make_reader(chunk_size=1024, max_fetched_bytes=2048)

    assert reader.read(1024) == FILE_CONTENT[:1024]
    assert reader.read(1024) == FILE_CONTENT[1024:2048]
    assert reader.fetched_bytes == 2048
    with pytest.raises(Cloud115RequestError, match="budget exceeded"):
        reader.read(1)
    assert len(seen) == 2


def test_budget_rejects_large_http_200_before_accepting_full_object():
    forty_gib = 40 * 1024 * 1024 * 1024
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"range ignored")
        )
    )
    reader = Cloud115RangeReader(
        "https://cdn.115.example/video.mp4",
        user_agent=UA,
        file_size=forty_gib,
        max_fetched_bytes=64 * 1024 * 1024,
        http_client=client,
    )

    with pytest.raises(Cloud115RequestError, match="unsafe http 200"):
        reader.read(1)
    assert reader.fetched_bytes == 0


def test_budget_allows_sparse_range_reads_across_40g_file():
    forty_gib = 40 * 1024 * 1024 * 1024
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        range_header = request.headers["Range"]
        seen.append(range_header)
        start_s, end_s = range_header.removeprefix("bytes=").split("-")
        start, end = int(start_s), int(end_s)
        return httpx.Response(
            206,
            content=b"x" * (end - start + 1),
            headers={"Content-Range": f"bytes {start}-{end}/{forty_gib}"},
        )

    reader = Cloud115RangeReader(
        "https://cdn.115.example/4k.mkv",
        user_agent=UA,
        file_size=forty_gib,
        chunk_size=1024,
        max_fetched_bytes=4096,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert reader.read(16) == b"x" * 16
    reader.seek(-16, 2)
    assert reader.read(16) == b"x" * 16
    assert seen == ["bytes=0-1023", f"bytes={forty_gib - 16}-{forty_gib - 1}"]
    assert reader.fetched_bytes == 1040


def test_reader_is_file_like_for_pyav():
    reader, _ = _make_reader()
    assert reader.readable() is True
    assert reader.seekable() is True
    assert reader.writable() is False
    reader.close()


@pytest.mark.parametrize(
    "content_range",
    ["", "bytes nope", f"bytes 1-10/{len(FILE_CONTENT)}"],
)
def test_reader_rejects_missing_malformed_or_wrong_start_content_range(content_range):
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"Content-Range": content_range} if content_range else {}
        return httpx.Response(206, content=b"x" * 10, headers=headers)

    reader = Cloud115RangeReader(
        "https://cdn.115.example/video.mp4", user_agent=UA,
        file_size=len(FILE_CONTENT), http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(Cloud115RequestError):
        reader.read(10)


def test_reader_rejects_short_and_empty_206_body():
    for body in (b"short", b""):
        def handler(request: httpx.Request, body=body) -> httpx.Response:
            return httpx.Response(
                206, content=body,
                headers={"Content-Range": f"bytes 0-9/{len(FILE_CONTENT)}"},
            )

        reader = Cloud115RangeReader(
            "https://cdn.115.example/video.mp4", user_agent=UA,
            file_size=len(FILE_CONTENT), http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(Cloud115RequestError):
            reader.read(10)


def test_reader_accepts_only_full_object_http_200_from_start_zero():
    full_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=FILE_CONTENT)
        )
    )
    reader = Cloud115RangeReader(
        "https://cdn.115.example/video.mp4", user_agent=UA,
        file_size=len(FILE_CONTENT), http_client=full_client,
    )
    assert reader.read(10) == FILE_CONTENT[:10]

    partial_client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=FILE_CONTENT[:10])
        )
    )
    unsafe = Cloud115RangeReader(
        "https://cdn.115.example/video.mp4", user_agent=UA,
        file_size=len(FILE_CONTENT), http_client=partial_client,
    )
    with pytest.raises(Cloud115RequestError, match="unsafe http 200"):
        unsafe.read(10)
