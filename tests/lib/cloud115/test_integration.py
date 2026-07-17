"""Cloud115Client 集成测试：打真实 115 端点。

默认 skip；需要显式 --run-cloud115-integration flag + COOKIE_115 环境变量：
    set -x COOKIE_115 "UID=...; CID=...; SEID=...; KID=..."
    uv run pytest tests/lib/cloud115/test_integration.py --run-cloud115-integration -n0 -v

用途：端到端验证 cipher 与 115 服务端协议一致（cipher 单元测试无法覆盖，
因为 rsa_encode 与 rsa_decode 不是数学逆变换）。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Callable

import httpx
import pytest

from src.lib.cloud115 import (
    Cloud115Client,
    Cloud115MembershipRequiredError,
    Cloud115NotFoundError,
    Cloud115VideoNotReadyError,
    DirEntry,
)
from src.service.playback.media_thumbnail_service import MediaThumbnailService

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


async def test_pickcode_info_real(client: Cloud115Client) -> None:
    """从 list_dir 拿一个文件的 pickcode，pickcode_info 应返回同 file_info 一致的 file_id / size / sha1。"""
    entries, _ = await client.list_dir("0", limit=100)
    file_entry: DirEntry | None = next((e for e in entries if not e.is_dir), None)
    if file_entry is None:
        first_dir = next((e for e in entries if e.is_dir), None)
        if first_dir is None:
            pytest.skip("root has neither files nor subdirs")
        sub, _ = await client.list_dir(first_dir.entry_id, limit=100)
        file_entry = next((e for e in sub if not e.is_dir), None)
        if file_entry is None:
            pytest.skip("no file reachable in 1 level")

    by_pc = await client.pickcode_info(file_entry.pickcode)
    by_fid = await client.file_info(file_entry.entry_id)
    assert by_pc.file_id == by_fid.file_id
    assert by_pc.pickcode == file_entry.pickcode
    assert by_pc.size == by_fid.size
    assert by_pc.sha1 == by_fid.sha1


async def test_dir_info_root_sentinel_real(client: Cloud115Client) -> None:
    """cid=0 不打服务端，直接返哨兵。"""
    d = await client.dir_info("0")
    assert d.cid == "0"
    assert d.name == "根目录"
    assert d.pickcode == ""
    assert d.paths == ()


async def test_dir_info_subdir_real(client: Cloud115Client) -> None:
    """从 list_dir 拿一个子目录 cid，dir_info 应返回 name / paths 面包屑。"""
    entries, _ = await client.list_dir("0", limit=100)
    sub: DirEntry | None = next((e for e in entries if e.is_dir), None)
    if sub is None:
        pytest.skip("no subdir at root")
    d = await client.dir_info(sub.entry_id)
    assert d.cid == sub.entry_id
    assert d.name == sub.name
    assert d.parent_id == "0"    # 父就是根，file_id 是数字 0
    assert len(d.paths) >= 1
    # 面包屑首个是根
    assert d.paths[0].name == "根目录"


async def test_offline_quota_real(client: Cloud115Client) -> None:
    q = await client.offline_quota()
    assert q.total > 0
    assert 0 <= q.remaining <= q.total


async def test_offline_default_dir_real(client: Cloud115Client) -> None:
    d = await client.default_download_dir()
    assert d.is_dir is True
    assert d.entry_id            # 目录 cid 非空
    assert d.name                # 目录名非空


async def test_offline_list_tasks_real(client: Cloud115Client) -> None:
    page = await client.list_offline_tasks(page=1, page_size=5)
    assert page.page >= 1
    assert page.page_count >= 1
    assert page.page_size > 0
    for t in page.tasks:
        assert t.info_hash              # info_hash 非空
        assert t.status in {-1, 0, 1, 2}
        assert t.retry_limit >= 0
        # 完成的任务应该有 file_id / pickcode；未完成的可能是空
        if t.status == 2:
            assert t.file_id
            assert t.pickcode


async def test_cookies_keepalive_refreshes_acw_tc_real(client: Cloud115Client, real_cookies: str) -> None:
    """初始不含 acw_tc 建 client，一次探活后 snapshot 应包含服务端塞回的新 acw_tc，
    并且新 acw_tc 能被下一次跨子域请求（webapi）复用。"""
    stripped = "; ".join(
        p.strip() for p in real_cookies.split(";") if not p.strip().startswith("acw_tc=")
    )
    async with Cloud115Client(cookies=stripped) as c:
        assert "acw_tc=" not in c.snapshot_cookies()
        await c.check_cookies_alive()
        snap = c.snapshot_cookies()
        assert "acw_tc=" in snap, "server should have Set-Cookie a fresh acw_tc"
        # 用刷回来的 acw_tc 跨子域请求 webapi
        entries, total = await c.list_dir("0", limit=5)
        assert total >= 0
        assert isinstance(entries, list)


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
    """VIP 专属：获取视频信息与可播放的多档清晰度。"""
    entries, _ = await client.list_dir("0", limit=200)
    video = next(
        (
            entry
            for entry in entries
            if not entry.is_dir
            and (
                entry.is_video
                or entry.name.lower().endswith((".mp4", ".mkv", ".ts", ".avi", ".mov"))
            )
        ),
        None,
    )
    if video is None:
        pytest.skip("no video file at root")
    try:
        info = await client.get_video_info(video.pickcode)
    except Cloud115MembershipRequiredError as exc:
        pytest.skip(f"account is not VIP: {exc}")
    assert info.master_m3u8_url.startswith("https://")
    assert info.width > 0
    assert info.height > 0
    assert info.definitions
    assert all(definition.m3u8_url.startswith("https://") for definition in info.definitions)
    assert all(definition.bandwidth > 0 for definition in info.definitions)


async def test_get_video_segments_real(client: Cloud115Client) -> None:
    """VIP 专属：获取最高码率 HLS 的 TS 分段列表。"""
    entries, _ = await client.list_dir("0", limit=200)
    video = next(
        (
            entry
            for entry in entries
            if not entry.is_dir
            and (
                entry.is_video
                or entry.name.lower().endswith((".mp4", ".mkv", ".ts", ".avi", ".mov"))
            )
        ),
        None,
    )
    if video is None:
        pytest.skip("no video file at root")
    try:
        segments = await client.get_video_segments(video.pickcode)
    except Cloud115MembershipRequiredError as exc:
        pytest.skip(f"account is not VIP: {exc}")
    assert segments
    assert sum(segment.duration_seconds for segment in segments) > 0
    assert [segment.index for segment in segments] == list(range(len(segments)))
    assert all(segment.url.startswith("https://") for segment in segments)


async def test_hls_segment_thumbnail_decode_real(
    real_cookies: str,
    tmp_path: Path,
) -> None:
    """真实 TS 使用绑定 UA 惰性读取，并能产出首个完整 WebP。"""
    async with Cloud115Client(
        cookies=real_cookies,
        user_agent=MediaThumbnailService.CLOUD115_THUMBNAIL_UA,
    ) as hls_client:
        pickcode = os.environ.get("CLOUD115_TEST_PICKCODE", "").strip()
        if not pickcode:
            entries, _ = await hls_client.list_dir("0", limit=200)
            video = next(
                (
                    entry
                    for entry in entries
                    if not entry.is_dir
                    and (
                        entry.is_video
                        or entry.name.lower().endswith(
                            (".mp4", ".mkv", ".ts", ".avi", ".mov")
                        )
                    )
                ),
                None,
            )
            if video is None:
                pytest.skip("no root video and CLOUD115_TEST_PICKCODE is unset")
            pickcode = video.pickcode

        try:
            info = await hls_client.get_video_info(pickcode)
        except Cloud115MembershipRequiredError as exc:
            pytest.skip(f"account is not VIP: {exc}")
        definition = MediaThumbnailService._select_lowest_hls_definition(
            info.definitions
        )
        segments = await hls_client.get_video_segments_for_definition(definition)
        if not segments:
            pytest.skip("video HLS playlist has no TS segments")

    generated_count = await asyncio.to_thread(
        MediaThumbnailService._decode_hls_segment_to_webp,
        segments[0],
        [0],
        tmp_path,
    )

    assert generated_count == 1
    assert (tmp_path / "0.webp").stat().st_size > 0


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


# ---------------------------------------------------------------------------
# HLS 协议解析回归：历史真机样本，不访问外网
# ---------------------------------------------------------------------------


_MOCK_COOKIE = "UID=12345678_A1_1700000000; CID=abc; SEID=xyz; KID=kkk"
_MASTER_M3U8_URL = "https://115.com/api/video/m3u8/cd5abc.m3u8"
_HD_M3U8_URL = "https://cpats01.115.com/xyz/HASH_1280.m3u8?u=1&s=4194304"
_MASTER_M3U8 = (
    "#EXTM3U\r\n"
    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1800000,RESOLUTION=1280x720,NAME="HD"\r\n'
    f"{_HD_M3U8_URL}\r\n"
    '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=800000,RESOLUTION=640x360,NAME="SD"\r\n'
    "https://cpats01.115.com/xyz/HASH_640.m3u8?u=1&s=4194304\r\n"
)
_VARIANT_M3U8 = (
    "#EXTM3U\r\n"
    "#EXTINF:10.000000,\r\n"
    "https://cpats01.115.com/xyz/HASH_1280-00001.ts?u=1&s=4194304\r\n"
    "#EXTINF:9.500000,\r\n"
    "/xyz/HASH_1280-00002.ts?u=1&s=4194304\r\n"
    "#EXTINF:5.123000,\r\n"
    "https://cpats01.115.com/xyz/HASH_1280-00003.ts?u=1&s=4194304\r\n"
    "#EXT-X-ENDLIST\r\n"
)
_VIDEO_INFO_PAYLOAD = {
    "state": True,
    "video_url": _MASTER_M3U8_URL,
    "thumb_url": "https://static.115.com/video/HASH.jpg",
    "width": "1280",
    "height": "720",
    "file_status": 1,
}


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    user_agent: str | None = None,
) -> Cloud115Client:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Cloud115Client(
        cookies=_MOCK_COOKIE,
        user_agent=user_agent,
        http_client=http_client,
    )


async def test_get_video_info_parses_historical_master_playlist() -> None:
    player_user_agent = "SakuraMedia-HLS-Test/1.0"

    def handler(request: httpx.Request) -> httpx.Response:
        # files/video 和后续 m3u8 必须使用同一播放器 UA，115 会把它写入 URL 签名。
        assert request.headers["User-Agent"] == player_user_agent
        if request.url.host == "webapi.115.com":
            assert request.url.path == "/files/video"
            assert request.url.params["pickcode"] == "cd5abc"
            return httpx.Response(200, json=_VIDEO_INFO_PAYLOAD)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_MASTER_M3U8.encode())
        raise AssertionError(f"unexpected request: {request.url}")

    client = _mock_client(handler, user_agent=player_user_agent)
    try:
        info = await client.get_video_info("cd5abc")
        assert info.width == 1280
        assert info.height == 720
        assert [definition.bandwidth for definition in info.definitions] == [1800000, 800000]
        assert info.definitions[0].label == "HD"
        assert info.definitions[0].m3u8_url == _HD_M3U8_URL
    finally:
        await client._client.aclose()


@pytest.mark.parametrize("file_status", [0, 2])
async def test_get_video_info_rejects_video_not_ready(file_status: int) -> None:
    client = _mock_client(
        lambda request: httpx.Response(
            200,
            json={"state": True, "file_status": file_status, "video_url": ""},
        )
    )
    try:
        with pytest.raises(Cloud115VideoNotReadyError) as error:
            await client.get_video_info("cd5abc")
        assert error.value.file_status == file_status
    finally:
        await client._client.aclose()


async def test_get_video_info_maps_membership_and_non_video_errors() -> None:
    membership_client = _mock_client(
        lambda request: httpx.Response(
            200,
            json={"state": False, "errno": 406, "error": "需要VIP会员"},
        )
    )
    non_video_client = _mock_client(
        lambda request: httpx.Response(
            200,
            json={"state": True, "video_url": "", "width": 0, "height": 0},
        )
    )
    try:
        with pytest.raises(Cloud115MembershipRequiredError):
            await membership_client.get_video_info("cd5abc")
        with pytest.raises(Cloud115NotFoundError, match="video_url missing"):
            await non_video_client.get_video_info("cd5abc")
    finally:
        await membership_client._client.aclose()
        await non_video_client._client.aclose()


async def test_get_video_segments_selects_highest_and_parses_relative_urls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "webapi.115.com":
            return httpx.Response(200, json=_VIDEO_INFO_PAYLOAD)
        if str(request.url) == _MASTER_M3U8_URL:
            return httpx.Response(200, content=_MASTER_M3U8.encode())
        if str(request.url) == _HD_M3U8_URL:
            return httpx.Response(200, content=_VARIANT_M3U8.encode())
        raise AssertionError(f"unexpected request: {request.url}")

    client = _mock_client(handler)
    try:
        segments = await client.get_video_segments("cd5abc")
        assert [segment.duration_seconds for segment in segments] == [10.0, 9.5, 5.123]
        assert segments[1].url == (
            "https://cpats01.115.com/xyz/HASH_1280-00002.ts?u=1&s=4194304"
        )
    finally:
        await client._client.aclose()


def test_m3u8_parsers_handle_missing_attributes_and_empty_input() -> None:
    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:RESOLUTION=1280x720\n"
        "relative.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=500000\n"
        "https://example.com/variant.m3u8\n"
    )
    definitions = Cloud115Client._parse_master_m3u8(
        master,
        base_url="https://example.com/master/index.m3u8",
    )
    assert definitions[0].bandwidth == 0
    assert definitions[0].m3u8_url == "https://example.com/master/relative.m3u8"
    assert definitions[1].resolution == ""
    assert definitions[1].label == ""
    assert Cloud115Client._parse_master_m3u8("", base_url="https://example.com") == []
    assert Cloud115Client._parse_variant_m3u8("", base_url="https://example.com") == []


def test_pick_variant_prefers_exact_bandwidth_then_highest() -> None:
    definitions = Cloud115Client._parse_master_m3u8(
        _MASTER_M3U8,
        base_url=_MASTER_M3U8_URL,
    )
    assert Cloud115Client._pick_variant(definitions, 800000).bandwidth == 800000
    assert Cloud115Client._pick_variant(definitions, 9999999).bandwidth == 1800000


# ---------------------------------------------------------------------------
# 文件管理接口（导入管线依赖）：递归枚举 / copy / rename / move / delete 全生命周期
# ---------------------------------------------------------------------------


async def _find_small_file(client: Cloud115Client, max_size: int = 50 * 1024 * 1024) -> DirEntry | None:
    """优先在前两层找小文件；找不到时递归，避免测试依赖用户目录布局。"""
    entries, _ = await client.list_dir("0", limit=200)
    files = [e for e in entries if not e.is_dir and 0 < e.size <= max_size]
    if files:
        return min(files, key=lambda e: e.size)
    for sub in (e for e in entries if e.is_dir):
        sub_entries, _ = await client.list_dir(sub.entry_id, limit=200)
        files = [e for e in sub_entries if not e.is_dir and 0 < e.size <= max_size]
        if files:
            return min(files, key=lambda e: e.size)
    count = 0
    async for entry in client.iter_files_recursive("0", page_size=1000):
        if 0 < entry.size <= max_size:
            return entry
        count += 1
        if count >= 5000:
            break
    return None


async def test_iter_files_recursive_real(client: Cloud115Client) -> None:
    """递归枚举根目录树（前 3 页封顶），验证 §8 清单：分页稳定 + play_long 出现在已转码视频上。"""
    count = 0
    seen_video_with_play_long = False
    async for entry in client.iter_files_recursive("0", page_size=1000):
        assert not entry.is_dir          # 递归模式只该出文件
        assert entry.entry_id
        assert entry.parent_id           # 每条都带 parent cid（拿不到父目录名，靠上层映射）
        if entry.is_video and entry.play_long:
            seen_video_with_play_long = True
        count += 1
        if count >= 3000:
            break
    if count == 0:
        pytest.skip("account has no files at all")
    # 只要账号里有已转码视频，play_long 就应该在递归响应里白给
    if not seen_video_with_play_long:
        pytest.skip("no transcoded video found to verify play_long presence")


async def test_copy_rename_move_delete_lifecycle_real(client: Cloud115Client) -> None:
    """写操作全生命周期（临时目录内完成，结束清理）：

    mkdir A/B → copy 小文件到 A（新 fid/pickcode、sha1 不变）→ 单文件 rename（fid 不变）
    → move A→B（fid/pickcode 不变）→ delete 清场。
    覆盖 §8 清单：copy 同步性（copy 后立即 re-list 能否看到）、单文件改名生效。
    """
    source = await _find_small_file(client)
    if source is None:
        pytest.skip("no small file (<50MB) found in first two levels for copy test")

    stamp = int(time.time())
    dir_a = await client.mkdir("0", f"sdk-it-a-{stamp}")
    dir_b = await client.mkdir("0", f"sdk-it-b-{stamp}")
    try:
        # 1) copy：产生新 fid/pickcode，sha1 不变
        await client.copy_files([source.entry_id], pid=dir_a)
        copied: DirEntry | None = None
        for _ in range(10):   # copy 是否同步落地未有官方承诺：轮询最多 ~10s
            entries, _ = await client.list_dir(dir_a, limit=50)
            copied = next((e for e in entries if not e.is_dir), None)
            if copied is not None:
                break
            time.sleep(1)
        assert copied is not None, "copied file did not appear in target dir within 10s"
        assert copied.sha1 == source.sha1
        assert copied.entry_id != source.entry_id, "copy must produce a NEW fid"
        assert copied.pickcode != source.pickcode, "copy must produce a NEW pickcode"

        # 2) 单文件改名：名字变、fid 不变；成功响应后按 fid 查询确认实际名称。
        suffix = source.name.rsplit(".", 1)[-1] if "." in source.name else ""
        new_name = f"renamed-{stamp}.{suffix}" if suffix else f"renamed-{stamp}"
        await client.rename_file(copied.entry_id, new_name)
        renamed = await client.file_info(copied.entry_id)
        assert renamed.name == new_name
        assert renamed.pickcode == copied.pickcode, "rename must keep pickcode"

        # 3) move：fid/pickcode 不变，位置变
        await client.move_files([copied.entry_id], pid=dir_b)
        entries_b, _ = await client.list_dir(dir_b, limit=50)
        moved = next((e for e in entries_b if e.entry_id == copied.entry_id), None)
        assert moved is not None, "moved file should appear in dir B"
        assert moved.pickcode == copied.pickcode, "move must keep pickcode"
        entries_a, _ = await client.list_dir(dir_a, limit=50)
        assert all(e.entry_id != copied.entry_id for e in entries_a), "file should have left dir A"
    finally:
        # 清场：删两个临时目录（rb/delete 对目录同样生效，进回收站）
        await client.delete_files([dir_a, dir_b])
    # 删除后 list 应报 NotFound 或列表为空（目录进了回收站）
    from src.lib.cloud115 import Cloud115NotFoundError

    try:
        entries, total = await client.list_dir(dir_a, limit=5)
        assert total == 0 or all(e.entry_id != dir_a for e in entries)
    except Cloud115NotFoundError:
        pass


async def test_cleanup_source_copy_then_delete_real(client: Cloud115Client) -> None:
    """只用原文件的临时复制品验证 cleanup-source 顺序，绝不删除用户原始文件。"""
    original = await _find_small_file(client)
    if original is None:
        pytest.skip("no small file (<50MB) found for cleanup-source test")

    stamp = int(time.time())
    temp_source_cid = await client.mkdir("0", f"sdk-it-cleanup-source-{stamp}")
    temp_target_cid: str | None = None
    try:
        temp_target_cid = await client.mkdir("0", f"sdk-it-cleanup-target-{stamp}")
        # 先把用户原文件复制为隔离的临时源，后续删除只针对这个副本。
        await client.copy_files([original.entry_id], pid=temp_source_cid)
        temp_source: DirEntry | None = None
        for _ in range(10):
            entries, _ = await client.list_dir(temp_source_cid, limit=50)
            temp_source = next((entry for entry in entries if not entry.is_dir), None)
            if temp_source is not None:
                break
            time.sleep(1)
        assert temp_source is not None

        # cleanup-source：复制到目标、单文件改名并按 fid 验证，最后才删除临时源。
        await client.copy_files([temp_source.entry_id], pid=temp_target_cid)
        target: DirEntry | None = None
        for _ in range(10):
            entries, _ = await client.list_dir(temp_target_cid, limit=50)
            target = next((entry for entry in entries if not entry.is_dir), None)
            if target is not None:
                break
            time.sleep(1)
        assert target is not None
        assert target.sha1 == temp_source.sha1 == original.sha1

        suffix = target.name.rsplit(".", 1)[-1] if "." in target.name else ""
        expected_name = f"cleanup-verified-{stamp}.{suffix}" if suffix else f"cleanup-verified-{stamp}"
        await client.rename_file(target.entry_id, expected_name)
        verified = await client.file_info(target.entry_id)
        assert verified.name == expected_name

        await client.delete_files([temp_source.entry_id])
        source_entries, _ = await client.list_dir(temp_source_cid, limit=50)
        assert all(entry.entry_id != temp_source.entry_id for entry in source_entries)
        # 清源完成后目标仍可查询，证明不是先移动或误删目标。
        surviving_target = await client.file_info(target.entry_id)
        assert surviving_target.name == expected_name
    finally:
        cleanup_cids = [temp_source_cid]
        if temp_target_cid is not None:
            cleanup_cids.append(temp_target_cid)
        await client.delete_files(cleanup_cids)


async def test_download_bytes_real(client: Cloud115Client) -> None:
    """download_bytes：找个 ≤5MB 小文件全量下载，长度与列表 size 一致。"""
    small = await _find_small_file(client, max_size=5 * 1024 * 1024)
    if small is None:
        pytest.skip("no file <=5MB found for download_bytes test")
    content = await client.download_bytes(
        small.pickcode,
        user_agent="SakuraMedia-SDK-IT/1.0",
        max_bytes=5 * 1024 * 1024,
    )
    assert len(content) == small.size
