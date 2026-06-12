from pathlib import Path

from src.common import content_fingerprint
from src.common.content_fingerprint import compute_content_fingerprint


def test_compute_content_fingerprint_reads_full_file_for_small_inputs(tmp_path, monkeypatch):
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"abcdefghijklmnopqrstuvwxyz")
    captured_ranges: list[tuple[int, int]] = []
    original = content_fingerprint._update_hash_with_range

    monkeypatch.setattr(content_fingerprint, "FULL_HASH_THRESHOLD_BYTES", 100)

    def _record_range(hasher, target_path: Path, start: int, end: int) -> None:
        captured_ranges.append((start, end))
        original(hasher, target_path, start, end)

    monkeypatch.setattr(content_fingerprint, "_update_hash_with_range", _record_range)

    compute_content_fingerprint(file_path, discriminator="ABP-123")

    assert captured_ranges == [(0, file_path.stat().st_size)]


def test_compute_content_fingerprint_uses_sparse_ranges_for_large_inputs(tmp_path, monkeypatch):
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(bytes(range(64)))
    captured_ranges: list[tuple[int, int]] = []
    original = content_fingerprint._update_hash_with_range

    monkeypatch.setattr(content_fingerprint, "FULL_HASH_THRESHOLD_BYTES", 16)
    monkeypatch.setattr(content_fingerprint, "SAMPLE_WINDOW_BYTES", 4)
    monkeypatch.setattr(content_fingerprint, "INTERIOR_SAMPLE_COUNT", 6)

    def _record_range(hasher, target_path: Path, start: int, end: int) -> None:
        captured_ranges.append((start, end))
        original(hasher, target_path, start, end)

    monkeypatch.setattr(content_fingerprint, "_update_hash_with_range", _record_range)

    compute_content_fingerprint(file_path, discriminator="ABP-123")

    assert captured_ranges == [
        (0, 4),
        (7, 11),
        (16, 20),
        (25, 29),
        (34, 38),
        (43, 47),
        (52, 56),
        (60, 64),
    ]


def test_compute_content_fingerprint_changes_with_discriminator(tmp_path):
    file_path = tmp_path / "clip.mp4"
    file_path.write_bytes(b"x" * 10)

    assert compute_content_fingerprint(file_path, discriminator="ABP-123") != compute_content_fingerprint(
        file_path, discriminator="SSIS-001"
    )
    # 无 discriminator 时仍然稳定可复现。
    assert compute_content_fingerprint(file_path) == compute_content_fingerprint(file_path)
