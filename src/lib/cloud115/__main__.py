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
    file-info                取单文件元信息（by file_id）
    pickcode-info            取单文件元信息（by pickcode，业务侧持久化的稳定 ID）
    dir-info                 取目录元信息 + 面包屑
    downurl                  拿 302 直链
    snapshot-cookies         打印当前最新 cookies 字符串（含服务端 Set-Cookie 更新）
    offline-list             列离线下载任务（分页）
    offline-quota            查本月离线下载次数配额
    offline-default-dir      查默认云下载保存目录
    offline-add              提交离线下载任务
    offline-del              删除离线任务
    offline-clear            按范围清空离线任务
    offline-restart          重试失败任务
    list-recursive           递归枚举目录树全部文件（导入管线用）
    copy                     批量复制文件/目录（云端零流量；产生新 fid/pickcode）
    move                     批量移动文件/目录（fid/pickcode 不变）
    rename                   单文件改名（文件新名必须带扩展名）
    delete                   批量删除文件/目录（进回收站）
    download-bytes           下载小文件到本地（字幕等；拿链接与 GET 自动同 UA）
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
    _print_file_meta(meta)
    return 0


async def _cmd_pickcode_info(client: Cloud115Client, args: argparse.Namespace) -> int:
    meta = await client.pickcode_info(args.pickcode)
    _print_file_meta(meta)
    return 0


async def _cmd_dir_info(client: Cloud115Client, args: argparse.Namespace) -> int:
    d = await client.dir_info(args.cid)
    _print_kv([
        ("cid", d.cid),
        ("name", d.name),
        ("pickcode", d.pickcode),
        ("parent_id", d.parent_id),
        ("file_count", d.file_count),
        ("folder_count", d.folder_count),
        ("play_long_seconds", d.play_long_seconds),
        ("mtime", d.mtime),
        ("ctime", d.ctime),
    ])
    print()
    print(f"  paths ({len(d.paths)}):")
    if not d.paths:
        print("    (root)")
    for p in d.paths:
        print(f"    file_id={p.file_id!r:<12}  name={p.name!r}")
    return 0


async def _cmd_snapshot_cookies(client: Cloud115Client, _args: argparse.Namespace) -> int:
    # 先探活一次触发一次 Set-Cookie merge，然后打印
    await client.check_cookies_alive()
    print(client.snapshot_cookies())
    return 0


# ---- 离线下载子命令 ----


async def _cmd_offline_list(client: Cloud115Client, args: argparse.Namespace) -> int:
    page = await client.list_offline_tasks(page=args.page, page_size=args.page_size)
    print(f"page {page.page}/{page.page_count}  page_size={page.page_size}  total={page.total_tasks}")
    if not page.tasks:
        print("  (empty)")
        return 0
    print()
    print(f"  {'status':<12}  {'progress':>8}  {'info_hash':<40}  name")
    print(f"  {'-'*12}  {'-'*8}  {'-'*40}  {'-'*30}")
    for t in page.tasks:
        prog = f"{t.percent_done:.1f}%"
        # 状态用 status_text 服务端已翻译好的中文
        st = t.status_text or {-1: "失败", 0: "待办", 1: "进行中", 2: "完成"}.get(t.status, str(t.status))
        print(f"  {st:<12}  {prog:>8}  {t.info_hash:<40}  {t.name}")
    return 0


async def _cmd_offline_quota(client: Cloud115Client, _args: argparse.Namespace) -> int:
    q = await client.offline_quota()
    _print_kv([("total", q.total), ("remaining", q.remaining), ("used", q.total - q.remaining)])
    return 0


async def _cmd_offline_default_dir(client: Cloud115Client, _args: argparse.Namespace) -> int:
    d = await client.default_download_dir()
    _print_kv([
        ("entry_id", d.entry_id),
        ("name", d.name),
        ("is_dir", d.is_dir),
        ("mtime", d.mtime),
    ])
    return 0


async def _cmd_offline_add(client: Cloud115Client, args: argparse.Namespace) -> int:
    save_dir_id = args.save_dir_id
    if not save_dir_id:
        # 用户没指定就用默认下载目录
        d = await client.default_download_dir()
        save_dir_id = d.entry_id
        print(f"(using default dir: cid={save_dir_id} name={d.name!r})")
    results = await client.add_offline_urls(args.url, save_dir_id=save_dir_id)
    for r in results:
        _print_kv([("info_hash", r.info_hash), ("url", r.url)])
        print()
    return 0


async def _cmd_offline_del(client: Cloud115Client, args: argparse.Namespace) -> int:
    await client.delete_offline_tasks(args.info_hash, delete_source_files=args.delete_source)
    print(f"deleted {len(args.info_hash)} task(s)  delete_source_files={args.delete_source}")
    return 0


async def _cmd_offline_clear(client: Cloud115Client, args: argparse.Namespace) -> int:
    await client.clear_offline_tasks(scope=args.scope)
    print(f"cleared scope={args.scope}")
    return 0


async def _cmd_offline_restart(client: Cloud115Client, args: argparse.Namespace) -> int:
    await client.restart_offline_task(args.info_hash)
    print(f"restarted {args.info_hash}")
    return 0


# ---- 文件管理子命令（导入管线用） ----


async def _cmd_list_recursive(client: Cloud115Client, args: argparse.Namespace) -> int:
    count = 0
    print(f"  {'fid':<20}  {'parent':<20}  {'size':>12}  {'play_long':>9}  {'ic':>2}  name")
    print(f"  {'-'*20}  {'-'*20}  {'-'*12}  {'-'*9}  {'-'*2}  {'-'*30}")
    async for e in client.iter_files_recursive(args.cid, page_size=args.page_size):
        pl = "-" if e.play_long is None else str(e.play_long)
        ic = "-" if e.ic is None else str(e.ic)
        print(f"  {e.entry_id:<20}  {e.parent_id:<20}  {e.size:>12,}  {pl:>9}  {ic:>2}  {e.name}")
        count += 1
        if args.max_files and count >= args.max_files:
            print(f"  ... stopped at --max-files={args.max_files}")
            break
    print(f"total shown: {count}")
    return 0


async def _cmd_copy(client: Cloud115Client, args: argparse.Namespace) -> int:
    await client.copy_files(args.fid, pid=args.pid)
    print(f"copied {len(args.fid)} item(s) -> pid={args.pid}（复制产生新 fid/pickcode，需 re-list 目标目录获取）")
    return 0


async def _cmd_move(client: Cloud115Client, args: argparse.Namespace) -> int:
    await client.move_files(args.fid, pid=args.pid)
    print(f"moved {len(args.fid)} item(s) -> pid={args.pid}（fid/pickcode 不变）")
    return 0


async def _cmd_rename(client: Cloud115Client, args: argparse.Namespace) -> int:
    await client.batch_rename({args.fid: args.new_name})
    print(f"renamed fid={args.fid} -> {args.new_name!r}")
    return 0


async def _cmd_delete(client: Cloud115Client, args: argparse.Namespace) -> int:
    await client.delete_files(args.fid, pid=args.pid)
    print(f"deleted {len(args.fid)} item(s)（进 115 回收站）")
    return 0


async def _cmd_download_bytes(client: Cloud115Client, args: argparse.Namespace) -> int:
    content = await client.download_bytes(
        args.pickcode, user_agent=args.user_agent, max_bytes=args.max_bytes
    )
    with open(args.output, "wb") as f:
        f.write(content)
    print(f"downloaded {len(content):,} bytes -> {args.output}")
    return 0


def _print_file_meta(meta) -> None:
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

    p_info = sub.add_parser("file-info", help="取单文件元信息（by file_id）")
    p_info.add_argument("--file-id", dest="file_id", required=True, help="文件 file_id（list-dir 结果里的 entry_id）")

    p_pi = sub.add_parser("pickcode-info", help="取单文件元信息（by pickcode）")
    p_pi.add_argument("--pickcode", required=True, help="文件 pickcode（业务侧持久化的稳定 ID）")

    p_di = sub.add_parser("dir-info", help="取目录元信息 + 面包屑")
    p_di.add_argument("--cid", required=True, help="目录 cid（根目录用 0，SDK 返回哨兵）")

    sub.add_parser("snapshot-cookies", help="打印当前最新 cookies 字符串（含服务端 Set-Cookie 更新）")

    p_ol = sub.add_parser("offline-list", help="列离线下载任务（分页）")
    p_ol.add_argument("--page", type=int, default=1, help="页码，从 1 开始（默认 1）")
    p_ol.add_argument("--page-size", dest="page_size", type=int, default=30, help="每页大小（默认 30）")

    sub.add_parser("offline-quota", help="查本月离线下载次数配额")

    sub.add_parser("offline-default-dir", help="查默认云下载保存目录")

    p_oa = sub.add_parser("offline-add", help="提交离线下载任务（--url 可重复）")
    p_oa.add_argument("--url", action="append", required=True,
                      help="下载源 URL，支持 http/https/ftp/magnet/ed2k；可重复传多个")
    p_oa.add_argument("--save-dir-id", dest="save_dir_id", default=None,
                      help="保存目录 cid；缺省时用默认云下载目录")

    p_od = sub.add_parser("offline-del", help="删除离线任务（--info-hash 可重复）")
    p_od.add_argument("--info-hash", dest="info_hash", action="append", required=True,
                      help="任务 info_hash；可重复传多个")
    p_od.add_argument("--delete-source", dest="delete_source", action="store_true",
                      help="同时删除已下载的源文件（不可逆）")

    p_oc = sub.add_parser("offline-clear", help="按范围清空离线任务")
    p_oc.add_argument("--scope", default="finished",
                      choices=["finished", "failed", "running", "all",
                               "finished_with_source", "all_with_source"],
                      help="清理范围（默认 finished）")

    p_or = sub.add_parser("offline-restart", help="重试失败/停滞的离线任务")
    p_or.add_argument("--info-hash", dest="info_hash", required=True, help="任务 info_hash")

    p_du = sub.add_parser("downurl", help="拿 302 直链")
    p_du.add_argument("--pickcode", required=True, help="文件 pickcode（list-dir 结果里 pc 列）")
    p_du.add_argument(
        "--user-agent",
        dest="user_agent",
        default="Mozilla/5.0 SakuraMedia-Cli/1.0",
        help="用于绑定 302 URL 的 UA；返回的 URL 里 f= 指纹与此 UA 绑定，"
             "后续 Range GET 必须复用同一 UA",
    )

    p_lr = sub.add_parser("list-recursive", help="递归枚举目录树全部文件（导入管线用）")
    p_lr.add_argument("--cid", default="0", help="起始目录 cid（默认 0 根目录）")
    p_lr.add_argument("--page-size", dest="page_size", type=int, default=1000,
                      help="分页大小（默认 1000，服务端最大 1150）")
    p_lr.add_argument("--max-files", dest="max_files", type=int, default=200,
                      help="最多打印多少条（默认 200；0 = 不限制）")

    p_cp = sub.add_parser("copy", help="批量复制文件/目录到目标目录（复制产生新 fid/pickcode）")
    p_cp.add_argument("--fid", action="append", required=True, help="源 fid/cid；可重复传多个")
    p_cp.add_argument("--pid", required=True, help="目标目录 cid")

    p_mv = sub.add_parser("move", help="批量移动文件/目录到目标目录（fid/pickcode 不变）")
    p_mv.add_argument("--fid", action="append", required=True, help="源 fid/cid；可重复传多个")
    p_mv.add_argument("--pid", required=True, help="目标目录 cid")

    p_rn = sub.add_parser("rename", help="单文件改名（文件新名必须带原扩展名）")
    p_rn.add_argument("--fid", required=True, help="目标 fid")
    p_rn.add_argument("--new-name", dest="new_name", required=True, help="新名字（文件必须带扩展名）")

    p_rm = sub.add_parser("delete", help="批量删除文件/目录（进 115 回收站）")
    p_rm.add_argument("--fid", action="append", required=True, help="fid/cid；可重复传多个")
    p_rm.add_argument("--pid", default=None, help="可选：删除项所在父目录 cid")

    p_db = sub.add_parser("download-bytes", help="下载小文件到本地（字幕等）")
    p_db.add_argument("--pickcode", required=True, help="文件 pickcode")
    p_db.add_argument("--output", required=True, help="本地输出路径")
    p_db.add_argument("--max-bytes", dest="max_bytes", type=int, default=10 * 1024 * 1024,
                      help="大小上限字节（默认 10MB，超限报错）")
    p_db.add_argument("--user-agent", dest="user_agent",
                      default="Mozilla/5.0 SakuraMedia-Cli/1.0",
                      help="下载 UA（拿链接与 GET 自动同 UA）")

    return parser


async def _run(args: argparse.Namespace) -> int:
    cookies = _resolve_cookies(args.cookies)
    handlers = {
        "check-alive": _cmd_check_alive,
        "list-dir": _cmd_list_dir,
        "file-info": _cmd_file_info,
        "pickcode-info": _cmd_pickcode_info,
        "dir-info": _cmd_dir_info,
        "downurl": _cmd_downurl,
        "snapshot-cookies": _cmd_snapshot_cookies,
        "offline-list": _cmd_offline_list,
        "offline-quota": _cmd_offline_quota,
        "offline-default-dir": _cmd_offline_default_dir,
        "offline-add": _cmd_offline_add,
        "offline-del": _cmd_offline_del,
        "offline-clear": _cmd_offline_clear,
        "offline-restart": _cmd_offline_restart,
        "list-recursive": _cmd_list_recursive,
        "copy": _cmd_copy,
        "move": _cmd_move,
        "rename": _cmd_rename,
        "delete": _cmd_delete,
        "download-bytes": _cmd_download_bytes,
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
