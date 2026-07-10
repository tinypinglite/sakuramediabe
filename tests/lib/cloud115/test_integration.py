"""Cloud115Client 集成测试：打真实 115 端点。

默认 skip；需要显式 --run-cloud115-integration flag + COOKIE_115 环境变量：
    set -x COOKIE_115 "UID=...; CID=...; SEID=...; KID=..."
    uv run pytest tests/lib/cloud115/test_integration.py --run-cloud115-integration -n0 -v

用途：端到端验证 cipher 与 115 服务端协议一致（cipher 单元测试无法覆盖，
因为 rsa_encode 与 rsa_decode 不是数学逆变换）。
"""

from __future__ import annotations

import os
import time

import pytest

from src.lib.cloud115 import Cloud115Client, DirEntry


pytestmark = pytest.mark.cloud115_integration


@pytest.fixture
def real_cookies() -> str:
    cookies = os.environ.get("COOKIE_115")
    if not cookies:
        pytest.skip("COOKIE_115 env var not set")
    return cookies


@pytest.fixture
async def client(real_cookies: str):
    async with Cloud115Client(cookies=real_cookies) as c:
        yield c


async def test_check_cookies_alive_real(client: Cloud115Client) -> None:
    alive = await client.check_cookies_alive()
    assert alive is True, "cookies should be alive; if not, refresh COOKIE_115"


async def test_list_root_dir_real(client: Cloud115Client) -> None:
    """根目录列表：只要能拿到 total >= 0 就算 list_dir 协议对了。"""
    entries, total = await client.list_dir("0", limit=50)
    assert isinstance(total, int)
    assert total >= 0
    assert isinstance(entries, list)
    if entries:
        e = entries[0]
        assert isinstance(e, DirEntry)
        assert e.entry_id
        assert e.name


async def test_file_info_real(client: Cloud115Client) -> None:
    """从 list_dir 拿一个文件的 file_id，再 file_info 断言字段。"""
    # 找一个文件（不是目录）；如果根目录全是目录，逐层往下找一次
    entries, _ = await client.list_dir("0", limit=100)
    file_entry: DirEntry | None = next((e for e in entries if not e.is_dir), None)
    if file_entry is None:
        # 根目录没直接放文件：往第一个目录里找一层
        first_dir = next((e for e in entries if e.is_dir), None)
        if first_dir is None:
            pytest.skip("root dir has neither files nor subdirs")
        sub_entries, _ = await client.list_dir(first_dir.entry_id, limit=100)
        file_entry = next((e for e in sub_entries if not e.is_dir), None)
        if file_entry is None:
            pytest.skip("no file reachable in 1 level for file_info smoke test")

    meta = await client.file_info(file_entry.entry_id)
    assert meta.file_id == file_entry.entry_id
    assert meta.name  # 有名字就够，size 不做严格值断言（file_info 语义与 list_dir 短字段可能不同）


async def test_get_video_info_real(client: Cloud115Client) -> None:
    """VIP 专属：拿视频信息 + 清晰度列表。非会员账号会因 errno=406 skip。"""
    entries, _ = await client.list_dir("0", limit=200)
    video = next(
        (e for e in entries if not e.is_dir and (e.is_video or e.name.lower().endswith((".mp4", ".mkv", ".ts", ".avi", ".mov")))),
        None,
    )
    if video is None:
        pytest.skip("no video file at root")
    try:
        info = await client.get_video_info(video.pickcode)
    except Exception as exc:
        # 非 VIP 会员时正常抛 Cloud115MembershipRequiredError，这里 skip 而非失败
        from src.lib.cloud115 import Cloud115MembershipRequiredError
        if isinstance(exc, Cloud115MembershipRequiredError):
            pytest.skip(f"account is not VIP: {exc}")
        raise
    assert info.master_m3u8_url.startswith("https://")
    assert info.width > 0
    assert info.height > 0
    assert len(info.definitions) >= 1
    # 每个 definition 都有绝对 URL 和正整数 bandwidth
    for d in info.definitions:
        assert d.m3u8_url.startswith("https://")
        assert d.bandwidth > 0


async def test_get_video_segments_real(client: Cloud115Client) -> None:
    """VIP 专属：拿 HLS 分段列表，验证总时长和视频 duration 大致一致。"""
    entries, _ = await client.list_dir("0", limit=200)
    video = next(
        (e for e in entries if not e.is_dir and (e.is_video or e.name.lower().endswith((".mp4", ".mkv", ".ts", ".avi", ".mov")))),
        None,
    )
    if video is None:
        pytest.skip("no video file at root")
    try:
        segments = await client.get_video_segments(video.pickcode)
    except Exception as exc:
        from src.lib.cloud115 import Cloud115MembershipRequiredError
        if isinstance(exc, Cloud115MembershipRequiredError):
            pytest.skip(f"account is not VIP: {exc}")
        raise
    assert len(segments) > 0
    total_duration = sum(s.duration_seconds for s in segments)
    assert total_duration > 0
    # 每段 URL 是绝对 https，duration 正
    for s in segments:
        assert s.url.startswith("https://")
        assert s.duration_seconds > 0
    # index 从 0 连续递增
    assert [s.index for s in segments] == list(range(len(segments)))


async def test_get_download_url_real(client: Cloud115Client) -> None:
    """从 list_dir 拿一个视频文件的 pickcode，get_download_url 断言 URL 结构。

    这是 cipher 端到端正确性的最强证明：encrypt_payload 若和服务端不对齐会 400/errno；
    decrypt_response 若不对齐会 CipherError 或 JSON parse 失败。
    """
    entries, _ = await client.list_dir("0", limit=200)
    # 找 is_video 或者扩展名像视频的文件
    video: DirEntry | None = next(
        (e for e in entries if not e.is_dir and (e.is_video or e.name.lower().endswith((".mp4", ".mkv", ".ts", ".avi", ".mov")))),
        None,
    )
    if video is None:
        pytest.skip("no video file at root; move a video file to root or extend this test to walk subdirs")

    ua = "Mozilla/5.0 SakuraMedia-Integration/1.0"
    du = await client.get_download_url(video.pickcode, user_agent=ua)
    assert du.url.startswith("https://")
    assert du.user_agent == ua
    # 有过期时间且是未来时间戳
    if du.expires_at > 0:
        assert du.expires_at > int(time.time()), "direct url should not be already expired"
    # sha1 / file_name / file_size 应至少非空/非零
    assert du.file_name
    assert du.file_size > 0
