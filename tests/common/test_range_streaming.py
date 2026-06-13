import pytest
from fastapi import HTTPException

from src.common.range_streaming import _get_range_header, _send_bytes_range_requests


def test_get_range_header_normal_range():
    assert _get_range_header("bytes=10-20", 100) == (10, 20)


def test_get_range_header_open_ended_range():
    # bytes=50- 表示从 50 到结尾。
    assert _get_range_header("bytes=50-", 100) == (50, 99)


def test_get_range_header_suffix_range_returns_tail():
    # bytes=-10 表示文件最后 10 字节，而非前 10 字节。
    assert _get_range_header("bytes=-10", 100) == (90, 99)


def test_get_range_header_suffix_larger_than_file_returns_whole_file():
    assert _get_range_header("bytes=-500", 100) == (0, 99)


def test_get_range_header_out_of_bounds_raises_416():
    with pytest.raises(HTTPException) as exc:
        _get_range_header("bytes=200-300", 100)
    assert exc.value.status_code == 416


def test_get_range_header_invalid_text_raises_416():
    with pytest.raises(HTTPException) as exc:
        _get_range_header("bytes=abc-def", 100)
    assert exc.value.status_code == 416


def test_get_range_header_zero_suffix_raises_416():
    with pytest.raises(HTTPException) as exc:
        _get_range_header("bytes=-0", 100)
    assert exc.value.status_code == 416


def test_send_bytes_range_requests_reads_exact_slice(tmp_path):
    data = bytes(range(100))
    file_path = tmp_path / "data.bin"
    file_path.write_bytes(data)

    # open 延迟到迭代时执行，迭代结束后句柄随 with 关闭。
    chunks = b"".join(_send_bytes_range_requests(str(file_path), 10, 19))

    assert chunks == data[10:20]
