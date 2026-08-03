"""mp4 虚拟合并（零落盘、可 Range seek）核心逻辑。

把 N 个同规格、同编码的本地 mp4（同属一部影片的多分段文件）在**不落盘**的前提下，
合并成一个逻辑上的单个 mp4：

- 逻辑文件 = ``ftyp + 合并moov + mdat_box``，mdat_box 的 payload 是各 part 的 mdat
  payload 按序连续拼接；源文件全程不动。
- 合并 moov 由每个 part 的 moov 合并而来：样本表(stts/stsz/stsc/stco/co64/ctts/stss/
  sgpd/sbgp)按序拼接，chunk 偏移重写到逻辑文件坐标，mvhd/tkhd/mdhd 时长换算到各自
  timescale。
- 播放器把它当一个带完整索引的普通 mp4：任意 Range 请求都能映射回某个源文件的精确
  字节区间（可 seek），且不产生任何临时文件。

仅支持「规整」输入：每个 part 恰好 1 个视频轨 + 0/1 个音频轨，各 part 的
stsd(含 avcC/hvcC) 与 mdhd timescale 完全一致（同规格同编码）。不满足抛
[Mp4MergeError]，由调用方转成 4xx。

本模块是纯逻辑，不依赖 DB/请求/配置，只面向字节，便于单测。
"""

from __future__ import annotations

import os
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field

# mp4 box 解析用；大端。
_BOX_HEADER = struct.Struct(">I4s")
_LARGE_SIZE_HEADER = struct.Struct(">Q")
# stco（32 位 chunk 偏移表）能表达的最大偏移，超出必须改用 co64。
_UINT32_MAX = 0xFFFF_FFFF


class Mp4MergeError(Exception):
    """合并不支持/合并失败。message 面向用户，error_code 供 ApiError 使用。"""

    def __init__(self, message: str, *, error_code: str = "merged_mp4_unsupported"):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


# ---------------------------------------------------------------------------
# box 遍历（基于 reader，不把整个大文件读进内存）
# ---------------------------------------------------------------------------


class FileReader:
    """基于 os.pread 的随机读 reader，供生产环境使用。"""

    def __init__(self, path: str):
        self._fd = os.open(path, os.O_RDONLY)
        self.size = os.fstat(self._fd).st_size

    def read(self, pos: int, n: int) -> bytes:
        if n <= 0:
            return b""
        return os.pread(self._fd, n, pos)

    def close(self) -> None:
        os.close(self._fd)


@dataclass(frozen=True)
class BoxRef:
    type: str
    header_len: int      # 8 或 16
    payload_start: int   # box payload 在 reader 中的绝对偏移
    end: int             # box 结束偏移（含 size 头）

    @property
    def total_len(self) -> int:
        return self.end - (self.payload_start - self.header_len)


def iter_boxes(reader: FileReader, start: int = 0, end: int | None = None) -> Iterable[BoxRef]:
    """按 box 顺序遍历 [start, end) 区间，产出 [BoxRef]。不读取 box 内容。"""
    if end is None:
        end = reader.size
    pos = start
    while pos + 8 <= end:
        header = reader.read(pos, 8)
        if len(header) < 8:
            break
        size, btype = _BOX_HEADER.unpack(header)
        header_len = 8
        if size == 1:
            large = reader.read(pos + 8, 8)
            if len(large) < 8:
                break
            (size,) = _LARGE_SIZE_HEADER.unpack(large)
            header_len = 16
        elif size == 0:
            size = end - pos
        payload_start = pos + header_len
        box_end = pos + size
        if box_end <= pos or box_end > end:
            break
        yield BoxRef(btype.decode("latin1"), header_len, payload_start, box_end)
        pos = box_end


# ---------------------------------------------------------------------------
# moov 解析（读入内存后解析）
# ---------------------------------------------------------------------------

_BOX_HEADER_BIG = struct.Struct(">I4s")


def _parse_boxes_bytes(data: bytes, start: int = 0, end: int | None = None) -> list[BoxRef]:
    """在已读入内存的字节里遍历 box，产出 BoxRef（payload_start 相对 data 起点）。"""
    boxes = []
    pos = start
    if end is None:
        end = len(data)
    while pos + 8 <= end:
        size, btype = _BOX_HEADER_BIG.unpack(data[pos:pos + 8])
        header_len = 8
        if size == 1:
            if pos + 16 > end:
                break
            (size,) = struct.unpack(">Q", data[pos + 8:pos + 16])
            header_len = 16
        elif size == 0:
            size = end - pos
        payload_start = pos + header_len
        box_end = pos + size
        if box_end <= pos or box_end > end:
            break
        boxes.append(BoxRef(btype.decode("latin1"), header_len, payload_start, box_end))
        pos = box_end
    return boxes


def _box_bytes(data: bytes, ref: BoxRef) -> bytes:
    """把 BoxRef 对应的完整 box（含头）切出来。"""
    return data[ref.payload_start - ref.header_len:ref.end]


@dataclass
class TrackInfo:
    kind: str = ""
    tkhd: bytes = b""
    mdhd: bytes = b""
    hdlr: bytes = b""
    stsd: bytes = b""
    stts: list[tuple[int, int]] = field(default_factory=list)
    stsc: list[tuple[int, int, int]] = field(default_factory=list)
    sample_sizes: list[int] = field(default_factory=list)
    fixed_size: int = 0
    sample_count: int = 0
    chunk_offsets: list[int] = field(default_factory=list)
    co64: bool = False
    ctts: list[tuple[int, int]] = field(default_factory=list)
    stss: list[int] = field(default_factory=list)
    sgpd: bytes | None = None
    sbgp: list[tuple[int, int]] = field(default_factory=list)
    sbgp_grouping: bytes | None = None
    minf_extras: list[bytes] = field(default_factory=list)
    stbl_extras: list[bytes] = field(default_factory=list)
    # 该 part 文件内 mdat payload 起点（供 chunk 偏移重写）
    mdat_payload_start: int = 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunk_offsets)


def _parse_track(moov: bytes, ref: BoxRef) -> TrackInfo | None:
    """解析一个 trak box（moov 是完整 moov 字节，ref 指向其中的 trak）。"""
    ti = TrackInfo()
    kids = {b.type: b for b in _parse_boxes_bytes(moov, ref.payload_start, ref.end)}
    tkhd = kids.get("tkhd")
    mdia = kids.get("mdia")
    if tkhd is None or mdia is None:
        return None
    ti.tkhd = _box_bytes(moov, tkhd)

    mdia_kids = {b.type: b for b in _parse_boxes_bytes(moov, mdia.payload_start, mdia.end)}
    mdhd = mdia_kids.get("mdhd")
    hdlr = mdia_kids.get("hdlr")
    minf = mdia_kids.get("minf")
    if mdhd is None or hdlr is None or minf is None:
        return None
    ti.mdhd = _box_bytes(moov, mdhd)
    hdlr_bytes = _box_bytes(moov, hdlr)
    ti.hdlr = hdlr_bytes
    handler = hdlr_bytes[16:20].decode("latin1")
    ti.kind = "video" if handler == "vide" else ("audio" if handler == "soun" else "")

    minf_kids = {b.type: b for b in _parse_boxes_bytes(moov, minf.payload_start, minf.end)}
    stbl = minf_kids.get("stbl")
    if stbl is None:
        return None
    for b in _parse_boxes_bytes(moov, minf.payload_start, minf.end):
        if b.type != "stbl":
            ti.minf_extras.append(_box_bytes(moov, b))

    for b in _parse_boxes_bytes(moov, stbl.payload_start, stbl.end):
        payload = moov[b.payload_start:b.end]
        btype = b.type
        if btype == "stsd":
            ti.stsd = _box_bytes(moov, b)
        elif btype == "stts":
            pos = 8
            while pos + 8 <= len(payload):
                ti.stts.append(struct.unpack(">II", payload[pos:pos + 8]))
                pos += 8
        elif btype == "stsc":
            pos = 8
            while pos + 12 <= len(payload):
                ti.stsc.append(struct.unpack(">III", payload[pos:pos + 12]))
                pos += 12
        elif btype == "stsz":
            fixed = struct.unpack(">I", payload[4:8])[0]
            count = struct.unpack(">I", payload[8:12])[0]
            ti.fixed_size = fixed
            ti.sample_count = count
            if fixed == 0:
                pos = 12
                for _ in range(count):
                    ti.sample_sizes.append(struct.unpack(">I", payload[pos:pos + 4])[0])
                    pos += 4
            else:
                ti.sample_sizes = [fixed] * count
        elif btype == "stco":
            pos = 8
            while pos + 4 <= len(payload):
                ti.chunk_offsets.append(struct.unpack(">I", payload[pos:pos + 4])[0])
                pos += 4
        elif btype == "co64":
            pos = 8
            while pos + 8 <= len(payload):
                ti.chunk_offsets.append(struct.unpack(">Q", payload[pos:pos + 8])[0])
                pos += 8
            ti.co64 = True
        elif btype == "ctts":
            pos = 8
            while pos + 8 <= len(payload):
                ti.ctts.append(struct.unpack(">II", payload[pos:pos + 8]))
                pos += 8
        elif btype == "stss":
            pos = 8
            while pos + 4 <= len(payload):
                ti.stss.append(struct.unpack(">I", payload[pos:pos + 4])[0])
                pos += 4
        elif btype == "sgpd":
            ti.sgpd = _box_bytes(moov, b)
        elif btype == "sbgp":
            ti.sbgp_grouping = payload[8:12]
            pos = 12
            while pos + 8 <= len(payload):
                ti.sbgp.append(struct.unpack(">II", payload[pos:pos + 8]))
                pos += 8
        else:
            ti.stbl_extras.append(_box_bytes(moov, b))
    return ti


# ---------------------------------------------------------------------------
# moov 合并
# ---------------------------------------------------------------------------


def _box(btype: str, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), btype.encode("latin1")) + payload


class _Mvhd:
    def __init__(self, raw: bytes):
        self.raw = bytearray(raw)
        self.version = raw[8]

    def timescale(self) -> int:
        if self.version == 1:
            return struct.unpack(">I", self.raw[28:32])[0]
        return struct.unpack(">I", self.raw[20:24])[0]

    def set_duration(self, value: int) -> bytes:
        if self.version == 1:
            struct.pack_into(">Q", self.raw, 32, value)
        else:
            struct.pack_into(">I", self.raw, 24, value)
        return bytes(self.raw)


class _Tkhd:
    def __init__(self, raw: bytes):
        self.raw = bytearray(raw)
        self.version = raw[8]

    def set_duration(self, value: int) -> bytes:
        if self.version == 1:
            struct.pack_into(">Q", self.raw, 36, value)
        else:
            struct.pack_into(">I", self.raw, 28, value)
        return bytes(self.raw)


class _Mdhd:
    def __init__(self, raw: bytes):
        self.raw = bytearray(raw)
        self.version = raw[8]

    def timescale(self) -> int:
        if self.version == 1:
            return struct.unpack(">I", self.raw[28:32])[0]
        return struct.unpack(">I", self.raw[20:24])[0]

    def duration(self) -> int:
        if self.version == 1:
            return struct.unpack(">Q", self.raw[32:40])[0]
        return struct.unpack(">I", self.raw[24:28])[0]

    def set_duration(self, value: int) -> bytes:
        if self.version == 1:
            struct.pack_into(">Q", self.raw, 32, value)
        else:
            struct.pack_into(">I", self.raw, 24, value)
        return bytes(self.raw)


def _pack_table(fmt: str, header: bytes, rows: list[tuple[int, ...]]) -> bytes:
    """把定宽行序列化为 box payload。

    ``rows`` 每行按 ``fmt`` 打包进预分配 bytearray；避免 ``bytes +=`` 在循环里
    造成 O(n²) 拷贝（10 万级样本表下会把合并播放构建拖到几十秒）。
    """
    size = struct.calcsize(fmt)
    buf = bytearray(len(header) + size * len(rows))
    buf[:len(header)] = header
    pos = len(header)
    for row in rows:
        struct.pack_into(fmt, buf, pos, *row)
        pos += size
    return bytes(buf)


def _build_stts(entries: list[tuple[int, int]]) -> bytes:
    return _box(
        "stts",
        _pack_table(">II", struct.pack(">II", 0, len(entries)), entries),
    )


def _build_stsc(entries: list[tuple[int, int, int]]) -> bytes:
    return _box(
        "stsc",
        _pack_table(">III", struct.pack(">II", 0, len(entries)), entries),
    )


def _build_stsz(sizes: list[int], fixed: int, count: int) -> bytes:
    header = struct.pack(">III", 0, fixed, count)
    if fixed == 0:
        return _box("stsz", _pack_table(">I", header, [(s,) for s in sizes]))
    return _box("stsz", header)


def _build_stco(offsets: list[int], co64: bool) -> bytes:
    if co64:
        return _box(
            "co64",
            _pack_table(">Q", struct.pack(">II", 0, len(offsets)), [(o,) for o in offsets]),
        )
    if offsets and max(offsets) > _UINT32_MAX:
        # 调用方应已在两遍法之前把 co64 判定好；走到这里说明判定失效，
        # 宁可显式报错也不能写出偏移被截断的 stco。
        raise Mp4MergeError("合并后 chunk 偏移超出 32 位范围，无法写入 stco")
    return _box(
        "stco",
        _pack_table(">I", struct.pack(">II", 0, len(offsets)), [(o,) for o in offsets]),
    )


def _build_ctts(entries: list[tuple[int, int]]) -> bytes:
    return _box(
        "ctts",
        _pack_table(">II", struct.pack(">II", 0, len(entries)), entries),
    )


def _build_stss(samples: list[int]) -> bytes:
    return _box(
        "stss",
        _pack_table(">I", struct.pack(">II", 0, len(samples)), [(s,) for s in samples]),
    )


def _build_sbgp(grouping: bytes, entries: list[tuple[int, int]]) -> bytes:
    return _box(
        "sbgp",
        _pack_table(
            ">II",
            struct.pack(">II", 0, len(entries)) + grouping,
            entries,
        ),
    )


def _serialize_track(t: TrackInfo) -> bytes:
    stbl_parts = [t.stsd, _build_stts(t.stts), _build_stsc(t.stsc)]
    stbl_parts.append(_build_stsz(t.sample_sizes, t.fixed_size, t.sample_count))
    stbl_parts.append(_build_stco(t.chunk_offsets, t.co64))
    if t.ctts:
        stbl_parts.append(_build_ctts(t.ctts))
    if t.stss:
        stbl_parts.append(_build_stss(t.stss))
    if t.sgpd:
        stbl_parts.append(t.sgpd)
    if t.sbgp and t.sbgp_grouping:
        stbl_parts.append(_build_sbgp(t.sbgp_grouping, t.sbgp))
    stbl_parts.extend(t.stbl_extras)
    stbl = _box("stbl", b"".join(stbl_parts))
    minf_extras = b"".join(t.minf_extras)
    minf = _box("minf", minf_extras + stbl)
    mdia = _box("mdia", t.mdhd + t.hdlr + minf)
    return _box("trak", t.tkhd + mdia)


def _merge_track(
    tr_parts: list[tuple[int, TrackInfo]],
    mdat_starts: list[int],
    force_co64: bool = False,
) -> TrackInfo:
    merged = TrackInfo()
    first = tr_parts[0][1]
    merged.kind = first.kind
    merged.tkhd = first.tkhd
    merged.mdhd = first.mdhd
    merged.hdlr = first.hdlr
    merged.stsd = first.stsd
    merged.minf_extras = first.minf_extras
    merged.fixed_size = first.fixed_size
    merged.sgpd = first.sgpd
    merged.sbgp_grouping = first.sbgp_grouping
    for _pi, t in tr_parts:
        merged.stts.extend(t.stts)
        merged.sample_sizes.extend(t.sample_sizes)
        merged.sample_count += t.sample_count
        merged.ctts.extend(t.ctts)
        merged.sbgp.extend(t.sbgp)
    sample_acc = 0
    for _pi, t in tr_parts:
        merged.stss.extend(s + sample_acc for s in t.stss)
        sample_acc += t.sample_count
    chunk_acc = 0
    for _pi, t in tr_parts:
        shifted = [(fc + chunk_acc, spc, sdi) for fc, spc, sdi in t.stsc]
        merged.stsc.extend(shifted)
        chunk_acc += t.chunk_count
    # chunk 偏移重写到逻辑文件坐标
    merged.chunk_offsets = []
    for pi, t in tr_parts:
        part_mdat_start = t.mdat_payload_start
        delta = mdat_starts[pi] - part_mdat_start
        merged.chunk_offsets.extend(o + delta for o in t.chunk_offsets)
    # 源文件本就是 64 位偏移，或合并后逻辑偏移越过 stco 上限，都必须写 co64。
    merged.co64 = force_co64 or any(t.co64 for _pi, t in tr_parts)
    return merged


def _sum_duration(tr_parts: list[tuple[int, TrackInfo]]) -> tuple[int, int]:
    ts = _Mdhd(tr_parts[0][1].mdhd).timescale()
    total = sum(_Mdhd(t.mdhd).duration() for _pi, t in tr_parts)
    return ts, total


# ---------------------------------------------------------------------------
# 顶层构建
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MdatPart:
    """逻辑文件中一段连续 payload 的来源：某个源文件的 mdat payload 区间。"""

    path: str
    payload_start: int
    payload_len: int


@dataclass(frozen=True)
class ParsedPart:
    path: str
    ftyp: bytes
    moov: bytes
    tracks: list[TrackInfo]
    mdat_payload_len: int = 0
    mdat_payload_start: int = 0


def _mdat_box_header(payload_len: int) -> bytes:
    """mdat box 头。payload 撑破 32 位 size 字段时改用 64 位 large size，头从 8 字节变 16 字节。"""
    if 8 + payload_len > _UINT32_MAX:
        return struct.pack(">I4sQ", 1, b"mdat", 16 + payload_len)
    return struct.pack(">I4s", 8 + payload_len, b"mdat")


@dataclass(frozen=True)
class MergedLayout:
    """合并后的逻辑 mp4 布局。header 与各 part 均为只读引用，不复制 mdat 数据。"""

    header: bytes                      # ftyp + 合并 moov
    parts: list[MdatPart]              # 逻辑顺序（mdat payload 连续拼接）
    # 段解析用：mdat box 头（8 或 16 字节）之后的逻辑偏移
    _mdat_payload_start: int = 0
    _mdat_header: bytes = b""

    def __post_init__(self):
        payload_len = sum(p.payload_len for p in self.parts)
        mdat_header = _mdat_box_header(payload_len)
        object.__setattr__(self, "_mdat_header", mdat_header)
        object.__setattr__(
            self, "_mdat_payload_start", len(self.header) + len(mdat_header)
        )

    @property
    def total_size(self) -> int:
        return (
            len(self.header)
            + len(self._mdat_header)
            + sum(p.payload_len for p in self.parts)
        )

    @property
    def mdat_payload_start(self) -> int:
        return self._mdat_payload_start

    def resolve_range(self, start: int, end: int) -> list[tuple[str, object, int, int]]:
        """把逻辑字节区间 [start, end)（半开）解析成段列表。

        段为 ``("mem", bytes, 0, 0)``（header / mdat box 头字节）或
        ``("file", path, file_offset, n)``（某 part 文件内 [file_offset, file_offset+n) 的字节）。
        """
        segments: list[tuple[str, object, int, int]] = []
        header_len = len(self.header)
        mdat_head_start = header_len
        mdat_head_end = mdat_head_start + len(self._mdat_header)
        pos = start
        if pos < mdat_head_end:
            seg_end = min(end, mdat_head_end)
            if pos < header_len:
                seg_end_h = min(seg_end, header_len)
                segments.append(("mem", self.header[pos:seg_end_h], 0, 0))
                pos = seg_end_h
            if pos < mdat_head_end:
                seg_end_m = min(seg_end, mdat_head_end)
                mh_start = pos - mdat_head_start
                segments.append(
                    ("mem", self._mdat_header[mh_start:seg_end_m - mdat_head_start], 0, 0)
                )
                pos = seg_end_m
        if pos >= end:
            return segments
        # mdat 区间
        mdat_begin = self.mdat_payload_start
        cursor = mdat_begin
        for part in self.parts:
            part_begin = cursor
            part_end = part_begin + part.payload_len
            if pos < part_end:
                seg_start = max(pos, part_begin)
                seg_end = min(end, part_end)
                if seg_start < seg_end:
                    file_off = part.payload_start + (seg_start - part_begin)
                    segments.append(("file", part.path, file_off, seg_end - seg_start))
                pos = seg_end
            cursor = part_end
            if pos >= end:
                break
        return segments


def parse_file(path: str) -> ParsedPart:
    """从文件解析：只读 ftyp/moov，定位各 mdat payload 区间（不读 mdat 数据）。"""
    reader = FileReader(path)
    try:
        ftyp: bytes | None = None
        moov: bytes | None = None
        mdat_payload_len = 0
        mdat_positions: list[tuple[int, int]] = []
        for box in iter_boxes(reader):
            if box.type == "ftyp":
                ftyp = reader.read(box.payload_start, box.end - box.payload_start)
            elif box.type == "moov":
                moov = reader.read(box.payload_start, box.end - box.payload_start)
            elif box.type == "mdat":
                mdat_positions.append((box.payload_start, box.end - box.payload_start))
        if ftyp is None or moov is None or not mdat_positions:
            raise Mp4MergeError("文件缺少 ftyp/moov/mdat 结构，无法合并播放")
        # 多个 mdat box 时按序累加（各 mdat payload 作为连续拼接来源）
        mdat_payload_len = sum(length for _s, length in mdat_positions)
        tracks = []
        for box in _parse_boxes_bytes(moov):
            if box.type == "trak":
                ti = _parse_track(moov, box)
                if ti is not None:
                    tracks.append(ti)
        # 记录该 part 内 mdat payload 起点（取全部轨第一个 chunk 的位置）
        min_chunks = [min(t.chunk_offsets) for t in tracks if t.chunk_offsets]
        mdat_payload_start = min(min_chunks) if min_chunks else mdat_positions[0][0]
        for t in tracks:
            t.mdat_payload_start = mdat_payload_start
        return ParsedPart(
            path=path,
            ftyp=ftyp,
            moov=moov,
            tracks=tracks,
            mdat_payload_len=mdat_payload_len,
            mdat_payload_start=mdat_payload_start,
        )
    finally:
        reader.close()


def _extract_audio_dsi(esds: bytes) -> bytes:
    """从 esds box 里提取 DecoderSpecificInfo（tag 0x05）的内容（如 AAC AudioSpecificConfig）。

    esds 的 ES/DecoderConfig 描述符含 avgBitrate/maxBitrate 等随时长而异的字段，
    只有 DecoderSpecificInfo 才是真正决定解码兼容性的配置。

    按 ISO/IEC 14496-1 描述符结构解析：ES_Descriptor(0x03) 跳过 ES_ID+flags 及 flags
    置位的可选字段（streamDependence/URL/OCR），DecoderConfigDescriptor(0x04) 跳过
    13 字节固定头（objectType+streamType+bufferSizeDB+maxBitrate+avgBitrate），只在
    这两类容器里递归子描述符找 tag 0x05；其它描述符内容视为裸数据不递归。这样提取
    结果与 maxBitrate 等随文件变化的字段无关，避免同规格文件被误判为不一致。
    """
    if len(esds) < 12:
        return b""
    data = esds[8:]  # 跳过 box 头(size+type)，内容是 version_flags + 描述符链
    pos = 4  # 跳过 version_flags

    def header_len(tag: int, start: int, end: int) -> int:
        """返回容器描述符的固定头字节数（跳过后才是子描述符序列）。"""
        if tag == 0x03:  # ES_Descriptor：ES_ID(2)+flags(1)+按 flags 置位的可选字段
            if start + 3 > end:
                return end - start
            flags = data[start + 2]
            skip = 3
            if flags & 0x80 and start + skip < end:  # streamDependenceFlag
                skip += 2
            if flags & 0x40 and start + skip < end:  # URL_Flag：URLlength(1)+URLstring
                skip += 1 + data[start + skip]
            if flags & 0x20 and start + skip < end:  # OCRstreamFlag
                skip += 2
            return min(skip, end - start)
        if tag == 0x04:  # DecoderConfigDescriptor：objectType/streamType/bufferSizeDB/maxBitrate/avgBitrate
            return 13
        return 0

    def walk(p: int, end: int) -> bytes | None:
        while p < end:
            tag = data[p]
            q = p + 1
            length = 0
            while q < end:
                b = data[q]
                q += 1
                length = (length << 7) | (b & 0x7F)
                if not (b & 0x80):
                    break
            content_start = q
            content_end = min(q + length, end)
            if tag == 0x05:  # DecoderSpecificInfo 即目标
                return data[content_start:content_end]
            if tag in (0x03, 0x04):
                nested = walk(
                    content_start + header_len(tag, content_start, content_end),
                    content_end,
                )
                if nested is not None:
                    return nested
            p = content_end
        return None

    return walk(pos, len(data)) or b""


def _spec_key(stsd: bytes) -> bytes:
    """stsd 的规格比较键。

    stsd 里除编码配置外还有码率统计（btrt）等会随时长/文件而异的 box，不能整段比较。
    视频取 宽+高+编码配置(avcC/hvcC 等，含 SPS/PPS)；音频取 声道数+采样位深+采样率+
    DecoderSpecificInfo(AAC 配置)。同规格返回相同键。
    """
    if len(stsd) < 40:
        return stsd
    entry = stsd[16:]  # 跳过 stsd fullbox(8) + entry_count(4)
    etype = entry[4:8]
    if etype in (b"avc1", b"avc3", b"hvc1", b"hev1", b"vp09", b"av01"):
        prefix = b"vid"
        start = 86          # VisualSampleEntry 固定头长度
        codec_ids = (b"avcC", b"hvcC", b"vpcC", b"av1C")
        # width(2)+height(2) 在 VisualSampleEntry 内 offset 32
        spec_head = entry[32:36]
    elif etype in (b"mp4a", b"enca"):
        prefix = b"aud"
        start = 36          # AudioSampleEntry 固定头长度
        codec_ids = (b"esds",)
        # channelcount(2)+samplesize(2)@24 + samplerate(4)@32
        spec_head = entry[24:28] + entry[32:36]
    else:
        return stsd
    codec = b""
    pos = start
    while pos + 8 <= len(entry):
        size = struct.unpack(">I", entry[pos:pos + 4])[0]
        if size < 8 or pos + size > len(entry):
            break
        if entry[pos + 4:pos + 8] in codec_ids:
            codec = entry[pos:pos + size]
            if prefix == b"aud":
                codec = _extract_audio_dsi(codec)
            break
        pos += size
    return prefix + spec_head + codec


def _validate_same_spec(parts: list[ParsedPart]) -> None:
    """校验各 part 同规格：视频轨 stsd(含编码配置) 与 mdhd timescale 完全一致；
    音频轨要么都有要么都无，且同样一致。"""
    v_stsd: bytes | None = None
    v_ts: int | None = None
    a_stsd: bytes | None = None
    a_ts: int | None = None
    has_audio = False
    for part in parts:
        vids = [t for t in part.tracks if t.kind == "video"]
        auds = [t for t in part.tracks if t.kind == "audio"]
        if len(vids) != 1:
            raise Mp4MergeError("分段必须恰好包含 1 个视频轨")
        v_key = _spec_key(vids[0].stsd)
        v_ts_now = _Mdhd(vids[0].mdhd).timescale()
        if v_stsd is None:
            v_stsd = v_key
            v_ts = v_ts_now
        elif v_key != v_stsd or v_ts_now != v_ts:
            raise Mp4MergeError(
                "分段视频规格不一致（编码/分辨率/帧率不同），不支持合并播放",
                error_code="merged_mp4_mismatched_spec",
            )
        if auds:
            has_audio = True
            a_key = _spec_key(auds[0].stsd)
            a_ts_now = _Mdhd(auds[0].mdhd).timescale()
            if a_stsd is None:
                a_stsd = a_key
                a_ts = a_ts_now
            elif a_key != a_stsd or a_ts_now != a_ts:
                raise Mp4MergeError(
                    "分段音频规格不一致，不支持合并播放",
                    error_code="merged_mp4_mismatched_spec",
                )
        elif has_audio:
            raise Mp4MergeError(
                "部分分段缺少音频轨，不支持合并播放",
                error_code="merged_mp4_mismatched_spec",
            )


def build_merged_layout(parts: list[ParsedPart]) -> MergedLayout:
    """合并 N 个已解析 part 为 [MergedLayout]。调用方负责先做规格校验。"""
    if len(parts) < 2:
        raise Mp4MergeError("合并播放至少需要 2 个分段", error_code="merged_mp4_need_at_least_two")

    _validate_same_spec(parts)

    mdat_lens = [p.mdat_payload_len for p in parts]
    ftyp = parts[0].ftyp
    # ftyp 在 header 里以完整 box（含 8 字节头）出现，长度要按完整 box 算。
    ftyp_len = len(ftyp) + 8
    base_moov = parts[0].moov

    # 各 part 的轨道只解析一次：_merge_track 与时长改写都作用在新建的合并 track 上，
    # 不会回写这里的对象，多遍构建复用同一份解析结果即可。
    vid_parts: list[tuple[int, TrackInfo]] = []
    aud_parts: list[tuple[int, TrackInfo]] = []
    for pi, part in enumerate(parts):
        for box in _parse_boxes_bytes(part.moov):
            if box.type != "trak":
                continue
            ti = _parse_track(part.moov, box)
            if ti is None:
                continue
            # mdat_payload_start 是文件级信息（parse_file 时计算），track 对象需回填，
            # 否则 chunk 偏移重写会用 0 当基准。
            ti.mdat_payload_start = part.mdat_payload_start
            if ti.kind == "video":
                vid_parts.append((pi, ti))
            elif ti.kind == "audio":
                aud_parts.append((pi, ti))
    if len(vid_parts) != len(parts):
        raise Mp4MergeError("分段视频轨缺失，不支持合并播放")
    if len(aud_parts) not in (0, len(parts)):
        raise Mp4MergeError("部分分段缺少音频轨，不支持合并播放")

    mvhd_ref = None
    extras: list[bytes] = []
    for box in _parse_boxes_bytes(base_moov):
        if box.type == "mvhd":
            mvhd_ref = box
        elif box.type != "trak":
            extras.append(_box_bytes(base_moov, box))
    if mvhd_ref is None:
        raise Mp4MergeError("moov 缺少 mvhd")
    mvhd_raw = _box_bytes(base_moov, mvhd_ref)
    mvhd_ts = _Mvhd(mvhd_raw).timescale()

    # 合并后的总时长：mdhd 用各自 media timescale，mvhd/tkhd 用 movie timescale。
    vts, vdur = _sum_duration(vid_parts)
    adur = _sum_duration(aud_parts)[1] if aud_parts else 0
    movie_duration = round((vdur / vts) * mvhd_ts) if vts else 0

    def build_once(mdat_starts: list[int], force_co64: bool) -> bytes:
        vmerged = _merge_track(vid_parts, mdat_starts, force_co64)
        amerged = _merge_track(aud_parts, mdat_starts, force_co64) if aud_parts else None
        # 时长必须写在合并后的 track 上：tkhd/mdhd 是 bytes，改各 part 的副本改不到这里。
        vmerged.tkhd = _Tkhd(vmerged.tkhd).set_duration(movie_duration)
        vmerged.mdhd = _Mdhd(vmerged.mdhd).set_duration(vdur)
        if amerged is not None:
            amerged.tkhd = _Tkhd(amerged.tkhd).set_duration(movie_duration)
            amerged.mdhd = _Mdhd(amerged.mdhd).set_duration(adur)

        trak_bytes = [_serialize_track(vmerged)]
        if amerged is not None:
            trak_bytes.append(_serialize_track(amerged))
        new_mvhd = _Mvhd(mvhd_raw).set_duration(movie_duration)
        return _box("moov", new_mvhd + b"".join(trak_bytes) + b"".join(extras))

    # 两遍法：偏移值本身不影响 box 长度，第一遍拿 moov 长度再定 mdat 起点。
    dummy_starts = [ftyp_len]
    for ln in mdat_lens:
        dummy_starts.append(dummy_starts[-1] + ln)

    # co64 必须在两遍法之前定死：32/64 位偏移表长度不同，两遍用不同宽度会算错 mdat 起点。
    # 逻辑文件总大小是所有 chunk 偏移的严格上界；升级 co64 只会让 moov 更长、判定单调不变，
    # 因此按 32 位试算一次即可定论（各分段单独 <4GB 但合计越界时，源 stco 会被升级为 co64）。
    # mdat 头宽度只取决于 payload 总量，与 moov 长度无关，可先于两遍法定死。
    mdat_total = sum(mdat_lens)
    mdat_header_len = len(_mdat_box_header(mdat_total))

    source_co64 = any(t.co64 for _pi, t in vid_parts + aud_parts)
    first_moov = build_once(dummy_starts, source_co64)
    est_total = ftyp_len + len(first_moov) + mdat_header_len + mdat_total
    need_co64 = source_co64 or est_total > _UINT32_MAX
    if need_co64 != source_co64:
        # 升级为 co64 后偏移表变宽、moov 变长，第一遍的长度要按新宽度重算。
        first_moov = build_once(dummy_starts, need_co64)

    payload_start = ftyp_len + len(first_moov) + mdat_header_len
    real_starts = [payload_start]
    for ln in mdat_lens:
        real_starts.append(real_starts[-1] + ln)
    merged_moov = build_once(real_starts, need_co64)

    header = _box("ftyp", ftyp) + merged_moov
    mdat_parts = []
    for part in parts:
        mdat_parts.append(
            MdatPart(
                path=part.path,
                payload_start=part.mdat_payload_start,
                payload_len=part.mdat_payload_len,
            )
        )
    return MergedLayout(header=header, parts=mdat_parts)
