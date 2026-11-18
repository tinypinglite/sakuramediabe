from itertools import islice

from src.common.range_streaming import _send_bytes_range_requests


def test_range_stream_stops_when_read_returns_empty(monkeypatch) -> None:
    class EmptyStream:
        def __enter__(self):
            return self

        def __exit__(self, *_exc) -> None:
            return None

        def seek(self, _offset) -> None:
            return None

        def tell(self) -> int:
            return 0

        def read(self, _size) -> bytes:
            return b""

    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: EmptyStream())

    assert list(islice(_send_bytes_range_requests("media.mp4", 0, 1), 2)) == []
