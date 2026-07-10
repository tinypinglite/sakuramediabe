"""Cloud115Client 手动测试脚本。

调用方式（三种传 cookies 的方式任选其一）：

    # 1. 命令行传（最直接，会出现在 shell history 里，注意场合）
    uv run python -m src.lib.cloud115 --cookies "UID=...; CID=..." check-alive

    # 2. 环境变量（最推荐，重复执行方便）
    set -x COOKIE_115 "UID=...; CID=..."
    uv run python -m src.lib.cloud115 check-alive
    uv run python -m src.lib.cloud115 list-dir --cid 0 --limit 20

    # 3. 交互式 prompt（不传 --cookies 且没有 COOKIE_115 时自动进入）
    uv run python -m src.lib.cloud115 check-alive
    # -> 提示: paste cookies:

支持的子命令：
    check-alive              探测 cookies 是否活着
    list-dir                 列目录一页
    file-info                取单文件元信息
    downurl                  拿 302 直链
    video-info               拿视频信息 + 清晰度列表（VIP 专属）
    video-segments           拿视频 HLS ts 分段列表（VIP 专属）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from src.lib.cloud115.client import Cloud115Client
from src.lib.cloud115.exceptions import Cloud115Error


def _resolve_cookies(cli_value: str | None) -> str:
    """按优先级解析 cookies：--cookies > 环境变量 COOKIE_115 > 交互 prompt。"""
    if cli_value:
        return cli_value.strip()
    env_value = os.environ.get("COOKIE_115")
    if env_value:
        return env_value.strip()
    # 交互式：一行贴入，不隐藏（cookies 长，用 getpass 反而看不见粘贴反馈）
    print("paste cookies (single line, e.g. 'UID=...; CID=...; SEID=...; KID=...'):", file=sys.stderr)
    line = sys.stdin.readline()
    if not line:
        print("error: no cookies provided", file=sys.stderr)
        sys.exit(2)
    return line.strip()


def _print_kv(pairs: list[tuple[str, Any]]) -> None:
    """人眼友好的键值对输出。"""
    width = max(len(k) for k, _ in pairs) if pairs else 0
    for k, v in pairs:
        print(f"  {k.ljust(width)}  {v}")


async def _cmd_check_alive(client: Cloud115Client, _args: argparse.Namespace) -> int:
    alive = await client.check_cookies_alive()
    print(f"cookies alive: {alive}")
    return 0 if alive else 1


async def _cmd_list_dir(client: Cloud115Client, args: argparse.Namespace) -> int:
    entries, total = await client.list_dir(args.cid, offset=args.offset, limit=args.limit)
    print(f"cid={args.cid} total={total} shown={len(entries)} offset={args.offset} limit={args.limit}")
    if not entries:
        print("  (empty)")
        return 0
    print()
    # 表头
    print(f"  {'kind':<4}  {'entry_id':<20}  {'pickcode':<24}  {'size':>12}  name")
    print(f"  {'-'*4}  {'-'*20}  {'-'*24}  {'-'*12}  {'-'*30}")
    for e in entries:
        kind = "DIR" if e.is_dir else ("VID" if e.is_video else "FILE")
        size_str = "-" if e.is_dir else f"{e.size:,}"
        print(f"  {kind:<4}  {e.entry_id:<20}  {e.pickcode:<24}  {size_str:>12}  {e.name}")
    return 0


async def _cmd_file_info(client: Cloud115Client, args: argparse.Namespace) -> int:
    meta = await client.file_info(args.file_id)
    _print_kv([
        ("file_id", meta.file_id),
        ("parent_id", meta.parent_id),
        ("name", meta.name),
        ("size", f"{meta.size:,}"),
        ("sha1", meta.sha1),
        ("pickcode", meta.pickcode),
        ("mtime", meta.mtime),
        ("ctime", meta.ctime),
        ("is_video", meta.is_video),
    ])
    return 0


async def _cmd_downurl(client: Cloud115Client, args: argparse.Namespace) -> int:
    du = await client.get_download_url(args.pickcode, user_agent=args.user_agent)
    _print_kv([
        ("file_id", du.file_id),
        ("file_name", du.file_name),
        ("file_size", f"{du.file_size:,}"),
        ("sha1", du.sha1),
        ("pickcode", du.pickcode),
        ("user_agent", du.user_agent),
        ("expires_at", du.expires_at),
        ("url", du.url),
    ])
    return 0


async def _cmd_video_info(client: Cloud115Client, args: argparse.Namespace) -> int:
    info = await client.get_video_info(args.pickcode)
    _print_kv([
        ("pickcode", info.pickcode),
        ("width", info.width),
        ("height", info.height),
        ("thumb_url", info.thumb_url),
        ("master_m3u8_url", info.master_m3u8_url),
    ])
    print()
    print(f"  definitions ({len(info.definitions)}):")
    if not info.definitions:
        print("    (none)")
        return 0
    print(f"    {'bandwidth':>10}  {'resolution':<12}  {'label':<8}  m3u8_url")
    print(f"    {'-'*10}  {'-'*12}  {'-'*8}  {'-'*30}")
    for d in info.definitions:
        url_short = d.m3u8_url if len(d.m3u8_url) < 60 else d.m3u8_url[:57] + "..."
        print(f"    {d.bandwidth:>10}  {d.resolution:<12}  {d.label:<8}  {url_short}")
    return 0


async def _cmd_video_segments(client: Cloud115Client, args: argparse.Namespace) -> int:
    segments = await client.get_video_segments(
        args.pickcode,
        prefer_bandwidth=args.prefer_bandwidth,
    )
    total_duration = sum(s.duration_seconds for s in segments)
    print(f"segments: {len(segments)}  total_duration: {total_duration:.1f}s ({total_duration/60:.1f}min)")
    if not segments:
        return 0
    print()
    # 默认只打印前 10 段（避免 191 段刷屏），--all 展开全部
    show = segments if args.all else segments[:10]
    print(f"  {'idx':>4}  {'dur(s)':>7}  url")
    print(f"  {'-'*4}  {'-'*7}  {'-'*40}")
    for s in show:
        url_short = s.url if len(s.url) < 100 else s.url[:97] + "..."
        print(f"  {s.index:>4}  {s.duration_seconds:>7.2f}  {url_short}")
    if not args.all and len(segments) > 10:
        print(f"  ... {len(segments) - 10} more (pass --all to show)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.lib.cloud115",
        description="Cloud115Client 手动测试脚本",
    )
    parser.add_argument(
        "--cookies",
        default=None,
        help="115 cookies 字符串，形如 'UID=...; CID=...; SEID=...; KID=...'。"
             "缺省时读环境变量 COOKIE_115；再缺省时交互式提示。",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP 超时秒（默认 30）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    sub.add_parser("check-alive", help="探测 cookies 是否活着")

    p_list = sub.add_parser("list-dir", help="列目录一页")
    p_list.add_argument("--cid", default="0", help="目录 category_id，根目录用 0（默认 0）")
    p_list.add_argument("--offset", type=int, default=0, help="分页 offset（默认 0）")
    p_list.add_argument("--limit", type=int, default=50, help="页大小（默认 50，服务端最大 1150）")

    p_info = sub.add_parser("file-info", help="取单文件元信息")
    p_info.add_argument("--file-id", dest="file_id", required=True, help="文件 file_id（list-dir 结果里的 entry_id）")

    p_du = sub.add_parser("downurl", help="拿 302 直链")
    p_du.add_argument("--pickcode", required=True, help="文件 pickcode（list-dir 结果里 pc 列）")
    p_du.add_argument(
        "--user-agent",
        dest="user_agent",
        default="Mozilla/5.0 SakuraMedia-Cli/1.0",
        help="用于绑定 302 URL 的 UA；返回的 URL 里 f= 指纹与此 UA 绑定，"
             "后续 Range GET 必须复用同一 UA",
    )

    p_vi = sub.add_parser("video-info", help="拿视频信息 + 清晰度列表（VIP 专属）")
    p_vi.add_argument("--pickcode", required=True, help="视频文件 pickcode")

    p_vs = sub.add_parser("video-segments", help="拿视频 HLS ts 分段列表（VIP 专属）")
    p_vs.add_argument("--pickcode", required=True, help="视频文件 pickcode")
    p_vs.add_argument(
        "--prefer-bandwidth",
        dest="prefer_bandwidth",
        type=int,
        default=None,
        help="想要的清晰度码率（bit/s）；缺省时挑最高码率",
    )
    p_vs.add_argument("--all", action="store_true", help="打印全部分段（默认只列前 10 段）")

    return parser


async def _run(args: argparse.Namespace) -> int:
    cookies = _resolve_cookies(args.cookies)
    handlers = {
        "check-alive": _cmd_check_alive,
        "list-dir": _cmd_list_dir,
        "file-info": _cmd_file_info,
        "downurl": _cmd_downurl,
        "video-info": _cmd_video_info,
        "video-segments": _cmd_video_segments,
    }
    async with Cloud115Client(cookies=cookies, timeout=args.timeout) as client:
        return await handlers[args.command](client, args)


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except Cloud115Error as exc:
        # SDK 层已知异常：只印一行，退出码 3
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
