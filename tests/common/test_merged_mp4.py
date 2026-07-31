"""merged_mp4 虚拟合并核心逻辑测试。

测试 mp4 用系统 ffmpeg 现场生成（CI 无 ffmpeg 时整体跳过）。覆盖：
解析、合并布局、Range 段映射与手工拼接一致性、规格门槛。
"""

import shutil
import struct
import subprocess

import pytest

from src.service.playback import merged_mp4 as m


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _make_part(tmp_path, name, duration, size="320x240", pattern="testsrc", freq=440):
    out = tmp_path / f"{name}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", f"{pattern}=duration={duration}:size={size}:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "faststart", "-y", str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def _materialize(layout):
    """把布局完整还原成字节，验证 resolve_range 与布局一致。"""
    data = bytearray()
    for kind, arg, off, ln in layout.resolve_range(0, layout.total_size):
        if kind == "mem":
            data += arg
        else:
            with open(arg, "rb") as f:
                f.seek(off)
                data += f.read(ln)
    return bytes(data)


def _hand_built(layout):
    expected = layout.header + struct.pack(
        ">I4s", 8 + sum(p.payload_len for p in layout.parts), b"mdat"
    )
    for part in layout.parts:
        with open(part.path, "rb") as f:
            f.seek(part.payload_start)
            expected += f.read(part.payload_len)
    return expected


def _build_esds(max_bitrate: int, *, es_flags: int = 0x00, url: bytes = b"", include_dsi: bool = True) -> bytes:
    """构造合法的 esds box：version_flags + ES_Descriptor(0x03) -> DecoderConfig(0x04) -> DSI(0x05)。

    max_bitrate 可调，用于验证 DSI 提取不受码率字段影响；es_flags 控制 ES_Descriptor
    可选字段（streamDependence/URL/OCR）是否出现。
    """
    dsi = b"\x11\x90" if include_dsi else b""
    dsi_descr = b"\x05\x80\x80\x80\x02" + dsi if include_dsi else b""
    dec_config = b"\x40\x15\x00\x00\x00" + struct.pack(">II", max_bitrate, 255999) + dsi_descr
    dec_descr = b"\x04\x80\x80\x80" + bytes([len(dec_config)]) + dec_config
    sl_descr = b"\x06\x80\x80\x80\x01\x02"
    es_head = b"\x00\x02" + bytes([es_flags])
    if es_flags & 0x80:
        es_head += b"\x00\x03"  # dependsOn_ES_ID
    if es_flags & 0x40:
        es_head += bytes([len(url)]) + url
    if es_flags & 0x20:
        es_head += b"\x00\x04"  # OCR_ES_Id
    es_content = es_head + dec_descr + sl_descr
    es_descr = b"\x03\x80\x80\x80" + bytes([len(es_content)]) + es_content
    payload = b"\x00\x00\x00\x00" + es_descr  # version_flags + 描述符链
    return struct.pack(">I4s", 8 + len(payload), b"esds") + payload


# SIVR-272 两个分段的真实 esds（仅 maxBitrate 不同，DSI 均为 AAC-LC 48kHz 立体声 11 90）
ESDS_SEG_MAX_BITRATE_DIFF = (
    bytes.fromhex(
        "0000003365736473000000000380808022000200048080801440150000000003f2be0003e7ff05808080021190068080800102"
    ),
    bytes.fromhex(
        "000000336573647300000000038080802200020004808080144015000000000405c90003e7ff05808080021190068080800102"
    ),
)


class TestExtractAudioDsi:
    """esds DecoderSpecificInfo 提取回归：必须按描述符结构解析，不能把 DecoderConfig 的
    码率字段误当描述符 walk（否则同规格分段会因 maxBitrate 差异被判为不一致）。"""

    def test_real_segments_extract_same_dsi(self):
        a, b = ESDS_SEG_MAX_BITRATE_DIFF
        assert m._extract_audio_dsi(a) == b"\x11\x90"
        assert m._extract_audio_dsi(b) == b"\x11\x90"

    def test_dsi_ignores_bitrate_fields(self):
        a = _build_esds(258750)
        b = _build_esds(263625)
        assert m._extract_audio_dsi(a) == m._extract_audio_dsi(b) == b"\x11\x90"

    def test_dsi_with_es_optional_flags(self):
        # streamDependence + URL + OCR 全置位，仍能挖到 DSI
        a = _build_esds(258750, es_flags=0xE0, url=b"x")
        b = _build_esds(263625, es_flags=0xE0, url=b"x")
        assert m._extract_audio_dsi(a) == m._extract_audio_dsi(b) == b"\x11\x90"

    def test_missing_dsi_returns_empty(self):
        assert m._extract_audio_dsi(_build_esds(258750, include_dsi=False)) == b""

    def test_short_esds_returns_empty(self):
        assert m._extract_audio_dsi(b"") == b""
        assert m._extract_audio_dsi(b"\x00\x00\x00\x10esds") == b""


@pytest.mark.skipif(not _have_ffmpeg(), reason="需要 ffmpeg 生成测试 mp4")
class TestMergedMp4Build:
    def test_layout_materializes_to_logical_file(self, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        parts = [m.parse_file(str(p1)), m.parse_file(str(p2))]
        layout = m.build_merged_layout(parts)

        assert layout.total_size == len(layout.header) + 8 + sum(
            p.payload_len for p in layout.parts
        )
        # resolve_range 全量还原 == 手工拼接的逻辑文件
        assert _materialize(layout) == _hand_built(layout)

    def test_partial_ranges_align(self, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        layout = m.build_merged_layout([m.parse_file(str(p1)), m.parse_file(str(p2))])
        full = _hand_built(layout)

        for start, end in [
            (0, 100),                                    # 纯头部
            (len(layout.header) - 10, len(layout.header) + 10),  # 头 + mdat 头交界
            (layout.mdat_payload_start, layout.mdat_payload_start + 500),  # part1 段
            (layout.total_size - 100, layout.total_size),  # 尾部
        ]:
            data = bytearray()
            for kind, arg, off, ln in layout.resolve_range(start, end):
                if kind == "mem":
                    data += arg
                else:
                    with open(arg, "rb") as f:
                        f.seek(off)
                        data += f.read(ln)
            assert bytes(data) == full[start:end]

    def test_need_at_least_two_parts(self, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        with pytest.raises(m.Mp4MergeError):
            m.build_merged_layout([m.parse_file(str(p1))])

    def test_mismatched_spec_rejected(self, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 2, size="640x480")
        with pytest.raises(m.Mp4MergeError) as exc:
            m.build_merged_layout([m.parse_file(str(p1)), m.parse_file(str(p2))])
        assert exc.value.error_code == "merged_mp4_mismatched_spec"

    def test_header_contains_merged_moov(self, tmp_path):
        p1 = _make_part(tmp_path, "p1", 2)
        p2 = _make_part(tmp_path, "p2", 3)
        layout = m.build_merged_layout([m.parse_file(str(p1)), m.parse_file(str(p2))])
        # header = ftyp + moov，moov 必须位于 mdat 前（faststart 布局，Pico 播放器硬性要求）
        assert layout.header[4:8] == b"ftyp"
        assert b"moov" in layout.header
