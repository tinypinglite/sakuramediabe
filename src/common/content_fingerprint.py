"""媒体文件内容指纹算法（纯文件级，JAV 导入与非 JAV 视频导入共用）。

小文件全量 hash，大文件抽样首尾与中间多个窗口，避免对超大视频整文件扫描。
``discriminator`` 用于把额外维度（如 JAV 番号）混入指纹；非 JAV 导入传空串即可。
"""

import hashlib
from pathlib import Path

CONTENT_FINGERPRINT_VERSION = "fingerprint-v1"
FULL_HASH_THRESHOLD_BYTES = 100 * 1024 * 1024
SAMPLE_WINDOW_BYTES = 5 * 1024 * 1024
INTERIOR_SAMPLE_COUNT = 6
HASH_READ_CHUNK_BYTES = 1024 * 1024


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠采样区间，避免重复读取同一段文件。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _sample_ranges(file_size: int) -> list[tuple[int, int]]:
    """为大文件生成首尾加中间若干窗口的抽样区间。"""
    ranges: list[tuple[int, int]] = [
        (0, min(file_size, SAMPLE_WINDOW_BYTES)),
        (max(0, file_size - SAMPLE_WINDOW_BYTES), file_size),
    ]
    half_window = SAMPLE_WINDOW_BYTES // 2
    for index in range(1, INTERIOR_SAMPLE_COUNT + 1):
        center = int(file_size * index / (INTERIOR_SAMPLE_COUNT + 1))
        start = max(0, center - half_window)
        end = min(file_size, start + SAMPLE_WINDOW_BYTES)
        start = max(0, end - SAMPLE_WINDOW_BYTES)
        ranges.append((start, end))
    return _merge_ranges(ranges)


def _update_hash_with_range(hasher, file_path: Path, start: int, end: int) -> None:
    """把指定文件区间增量写入哈希器。"""
    remaining = max(0, end - start)
    if remaining == 0:
        return
    with file_path.open("rb") as file_handle:
        file_handle.seek(start)
        while remaining > 0:
            chunk = file_handle.read(min(HASH_READ_CHUNK_BYTES, remaining))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)


def compute_content_fingerprint(file_path: Path, *, discriminator: str = "") -> str:
    """计算文件内容指纹；``discriminator`` 混入额外维度，缺省为空。"""
    resolved_path = file_path.expanduser().resolve()
    file_size = resolved_path.stat().st_size
    hasher = hashlib.sha256()
    hasher.update(f"{CONTENT_FINGERPRINT_VERSION}\0".encode())
    hasher.update(f"{file_size}\0".encode())
    hasher.update(f"{discriminator}\0".encode())

    if file_size <= FULL_HASH_THRESHOLD_BYTES:
        _update_hash_with_range(hasher, resolved_path, 0, file_size)
    else:
        for start, end in _sample_ranges(file_size):
            _update_hash_with_range(hasher, resolved_path, start, end)

    return hasher.hexdigest()
