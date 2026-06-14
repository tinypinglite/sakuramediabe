from datetime import datetime

from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService


def test_resolve_creation_time_parses_iso8601_z_to_utc_naive():
    # 容器 metadata 的 Z 结尾时间戳应收口为 UTC naive。
    result = MediaMetadataProbeService._resolve_creation_time(
        {"creation_time": "2023-01-15T10:30:00.000000Z"}
    )
    assert result == datetime(2023, 1, 15, 10, 30, 0)


def test_resolve_creation_time_converts_timezone_offset_to_utc():
    # 带时区偏移的时间应换算到 UTC（+09:00 → 减 9 小时）。
    result = MediaMetadataProbeService._resolve_creation_time(
        {"creation_time": "2023-01-15T10:30:00+09:00"}
    )
    assert result == datetime(2023, 1, 15, 1, 30, 0)


def test_resolve_creation_time_falls_back_to_stream_metadata():
    # 容器 metadata 缺该键时回退到视频流 metadata。
    result = MediaMetadataProbeService._resolve_creation_time(
        {"encoder": "x264"},
        {"creation_time": "2022-06-01T00:00:00Z"},
    )
    assert result == datetime(2022, 6, 1, 0, 0, 0)


def test_resolve_creation_time_prefers_first_source():
    # 多来源均有时按入参优先级取首个（容器优先于视频流）。
    result = MediaMetadataProbeService._resolve_creation_time(
        {"creation_time": "2023-01-15T10:30:00Z"},
        {"creation_time": "2020-01-01T00:00:00Z"},
    )
    assert result == datetime(2023, 1, 15, 10, 30, 0)


def test_resolve_creation_time_returns_none_when_absent():
    assert MediaMetadataProbeService._resolve_creation_time({"encoder": "x264"}) is None
    assert MediaMetadataProbeService._resolve_creation_time(None) is None
    assert MediaMetadataProbeService._resolve_creation_time() is None


def test_resolve_creation_time_returns_none_for_invalid_value():
    # 非法字符串解析失败应如实返回 None 且不抛异常。
    assert MediaMetadataProbeService._resolve_creation_time({"creation_time": "garbage"}) is None
    assert MediaMetadataProbeService._resolve_creation_time({"creation_time": ""}) is None
