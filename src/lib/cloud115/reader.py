"""115 直链的受控顺序 Range 读取器（同步 file-like，喂给 PyAV/ffmpeg 抽帧）。

背景（真机实测，见 types.DirectUrl 注释）：单条直链顺序 Range 读很稳（整片抽 190 帧
只需一条链、198 个 Range 全 206），但**每条链约 4 并发上限**——把 ffmpeg/PyAV 默认
http 解复用器直接指向直链，它 open/seek 的突发并发请求会撞上限并被当作数据损坏报
InvalidData。本类是纪要拍板的替代方案：一次只发一个 Range 请求的受控数据源，
`av.open(reader)` 直接可用。

- 拿直链时绑定的 UA 与本类发出的每个 Range 请求必须一字不差一致（构造参数 user_agent）。
- 直链有 t= 过期（实测十几小时），本类不做刷新：403/过期抛 Cloud115RequestError，
  由上层决定重新拿链重试还是判失败。
- `max_fetched_bytes` 可限制累计响应字节数；超预算抛错，且超大 HTTP 200 整体响应不会读 body。
"""

from __future__ import annotations

import re

import httpx

from src.lib.cloud115.exceptions import Cloud115RequestError

# 单次 Range 请求的默认块大小：4MB 在实测中兼顾请求数与首帧延迟。
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024
_CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class Cloud115RangeReader:
    """115 直链的顺序 Range file-like reader（read/seek/tell，PyAV 可直接打开）。

    严格一次一个在途请求，天然满足"每链 ≤4 并发"约束；seek 只移动游标不发请求，
    read 命中缓冲直接返回、未命中才拉取一个块。
    """

    def __init__(
        self,
        url: str,
        *,
        user_agent: str,
        file_size: int,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        max_fetched_bytes: int | None = None,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not url:
            raise ValueError("url is required")
        if not user_agent:
            raise ValueError("user_agent is required (must match the UA bound to the URL)")
        if file_size <= 0:
            raise ValueError("file_size must be positive")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_fetched_bytes is not None and max_fetched_bytes <= 0:
            raise ValueError("max_fetched_bytes must be positive when provided")
        self._url = url
        self._user_agent = user_agent
        self._file_size = file_size
        self._chunk_size = chunk_size
        self._max_fetched_bytes = max_fetched_bytes
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout, trust_env=False)
        self._position = 0
        self._buffer = b""
        self._buffer_start = 0
        # 观测用：外部可断言请求次数（受控性验证）。
        self.request_count = 0
        self.fetched_bytes = 0

    # ---- file-like 协议 ----

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def file_size(self) -> int:
        return self._file_size

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self._position = offset
        elif whence == 1:
            self._position += offset
        elif whence == 2:
            self._position = self._file_size + offset
        else:
            raise ValueError(f"unsupported whence {whence}")
        self._position = max(0, self._position)
        return self._position

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self._file_size - self._position
        if size <= 0 or self._position >= self._file_size:
            return b""
        out = bytearray()
        while size > 0 and self._position < self._file_size:
            buffer_offset = self._position - self._buffer_start
            if 0 <= buffer_offset < len(self._buffer):
                take = min(size, len(self._buffer) - buffer_offset)
                out += self._buffer[buffer_offset:buffer_offset + take]
                self._position += take
                size -= take
                continue
            self._fetch(self._position, size)
        return bytes(out)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "Cloud115RangeReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ---- 内部 ----

    def _fetch(self, start: int, want: int) -> None:
        """拉一个覆盖 [start, ...) 的块进缓冲。want 超过 chunk_size 时一次拉够，避免碎请求。"""
        fetch_size = max(self._chunk_size, want)
        if self._max_fetched_bytes is not None:
            remaining_budget = self._max_fetched_bytes - self.fetched_bytes
            if remaining_budget <= 0:
                raise Cloud115RequestError(
                    f"range read budget exceeded ({self._max_fetched_bytes} bytes)",
                    method="GET",
                    url=self._url,
                    detail=f"fetched_bytes={self.fetched_bytes}",
                )
            fetch_size = min(fetch_size, remaining_budget)
        end = min(start + fetch_size, self._file_size) - 1
        try:
            with self._client.stream(
                "GET",
                self._url,
                headers={
                    "User-Agent": self._user_agent,
                    "Range": f"bytes={start}-{end}",
                },
            ) as response:
                if response.status_code not in (200, 206):
                    # 403 常见于直链过期或 UA 不一致；上层决定重取直链或判失败。
                    raise Cloud115RequestError(
                        f"http {response.status_code} on range {start}-{end} (expired url or UA mismatch?)",
                        method="GET", url=self._url,
                        detail=f"status={response.status_code}",
                    )

                raw_content_range = response.headers.get("Content-Range", "")
                if response.status_code == 206:
                    match = _CONTENT_RANGE_PATTERN.fullmatch(raw_content_range.strip())
                    if match is None:
                        raise Cloud115RequestError(
                            f"invalid Content-Range on range {start}-{end}",
                            method="GET", url=self._url, detail=raw_content_range,
                        )
                    actual_start, actual_end, actual_total = map(int, match.groups())
                    expected_length = actual_end - actual_start + 1
                    if (
                        actual_start != start
                        or actual_end < actual_start
                        or actual_end > end
                        or actual_total != self._file_size
                    ):
                        raise Cloud115RequestError(
                            f"inconsistent range response for {start}-{end}",
                            method="GET",
                            url=self._url,
                            detail=(
                                f"content_range={raw_content_range!r}, "
                                f"file_size={self._file_size}"
                            ),
                        )
                else:
                    # 有预算时先看文件总大小，拒绝读取忽略 Range 的超大 200 响应体。
                    remaining_budget = (
                        None
                        if self._max_fetched_bytes is None
                        else self._max_fetched_bytes - self.fetched_bytes
                    )
                    if start != 0 or (
                        remaining_budget is not None and self._file_size > remaining_budget
                    ):
                        raise Cloud115RequestError(
                            f"unsafe http 200 response for range {start}-{end}",
                            method="GET",
                            url=self._url,
                            detail=(
                                f"file_size={self._file_size}, "
                                f"remaining_budget={remaining_budget}"
                            ),
                        )

                content = response.read()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
            raise Cloud115RequestError(
                f"range request failed at {start}-{end}: {exc}",
                method="GET", url=self._url, detail=str(exc),
            ) from exc
        if response.status_code == 206:
            if (
                len(content) != expected_length
                or not content
            ):
                raise Cloud115RequestError(
                    f"inconsistent range response for {start}-{end}",
                    method="GET",
                    url=self._url,
                    detail=(
                        f"content_range={raw_content_range!r}, body_length={len(content)}, "
                        f"file_size={self._file_size}"
                    ),
                )
        elif len(content) != self._file_size:
            # 仅兼容忽略 Range、但从 0 返回完整对象的服务端；部分 200 无法安全定位。
            raise Cloud115RequestError(
                f"unsafe http 200 response for range {start}-{end}",
                method="GET",
                url=self._url,
                detail=f"body_length={len(content)}, file_size={self._file_size}",
            )
        if not content:
            raise Cloud115RequestError(
                f"empty range response before EOF at {start}",
                method="GET", url=self._url, detail="empty body",
            )
        self._buffer = content
        self._buffer_start = start
        self.request_count += 1
        self.fetched_bytes += len(content)
