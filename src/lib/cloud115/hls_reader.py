"""115 HLS TS 分片的前向惰性读取器。"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
from typing_extensions import Self

from src.lib.cloud115.exceptions import Cloud115RequestError

# MPEG-TS 包长 188 字节；网络块无需按包对齐，PyAV 会在内部完成拼包。
DEFAULT_HLS_STREAM_CHUNK_SIZE = 64 * 1024


class Cloud115HlsSegmentReader:
    """把单个 TS URL 暴露为只读、不可 seek 的同步 file-like。

    响应体只有在 ``read`` 被调用时才继续消费。上层解出首个完整帧后关闭 reader，
    httpx 会立刻关闭响应，未消费的分片内容不会继续下载。
    """

    def __init__(
        self,
        url: str,
        *,
        user_agent: str,
        chunk_size: int = DEFAULT_HLS_STREAM_CHUNK_SIZE,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not url:
            raise ValueError("url is required")
        if not user_agent:
            raise ValueError("user_agent is required (must match the UA bound to HLS)")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")

        self._url = url
        self._user_agent = user_agent
        self._chunk_size = chunk_size
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout, trust_env=False)
        self._stream_context = None
        self._response: httpx.Response | None = None
        self._iterator: Iterator[bytes] | None = None
        self._buffer = bytearray()
        self._position = 0
        self._eof = False
        self._closed = False

        # 仅用于日志与测试确认“按需读取后提前关闭”的行为。
        self.request_count = 0
        self.fetched_bytes = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._position

    @property
    def closed(self) -> bool:
        return self._closed

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            raise ValueError("I/O operation on closed Cloud115HlsSegmentReader")
        if size == 0:
            return b""

        self._ensure_open()
        if size is None or size < 0:
            self._consume_to_eof()
            size = len(self._buffer)
        else:
            self._fill_buffer(size)

        take = min(size, len(self._buffer))
        content = bytes(self._buffer[:take])
        del self._buffer[:take]
        self._position += take
        return content

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._stream_context is not None:
                self._stream_context.__exit__(None, None, None)
        finally:
            self._stream_context = None
            self._response = None
            self._iterator = None
            self._buffer.clear()
            if self._owns_client:
                self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._response is not None:
            return
        try:
            self._stream_context = self._client.stream(
                "GET",
                self._url,
                headers={"User-Agent": self._user_agent},
            )
            response = self._stream_context.__enter__()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            self.close()
            raise Cloud115RequestError(
                f"hls segment request failed: {exc}",
                method="GET",
                url=self._url,
                detail=str(exc),
            ) from exc

        if response.status_code not in (200, 206):
            status = response.status_code
            self.close()
            raise Cloud115RequestError(
                f"http {status} on HLS segment (expired URL or UA mismatch?)",
                method="GET",
                url=self._url,
                detail=f"status={status}",
            )

        self._response = response
        self._iterator = response.iter_bytes(chunk_size=self._chunk_size)
        self.request_count = 1

    def _fill_buffer(self, minimum_size: int) -> None:
        while len(self._buffer) < minimum_size and not self._eof:
            self._consume_next_chunk()

    def _consume_to_eof(self) -> None:
        while not self._eof:
            self._consume_next_chunk()

    def _consume_next_chunk(self) -> None:
        if self._iterator is None:
            self._eof = True
            return
        try:
            chunk = next(self._iterator)
        except StopIteration:
            self._eof = True
            return
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise Cloud115RequestError(
                f"hls segment stream failed: {exc}",
                method="GET",
                url=self._url,
                detail=str(exc),
            ) from exc
        if not chunk:
            return
        self._buffer.extend(chunk)
        self.fetched_bytes += len(chunk)
