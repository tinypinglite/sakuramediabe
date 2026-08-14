"""cloud115 离线下载提交。

设计要点：
- 所有 115 下载统一走磁力：candidate 只有 torrent_url 时先拉种子字节、解 info_hash、拼标准磁力，
  不走 BT 选文件三步（上传种子/解析/选文件）——广告文件由导入管线的扩展名白名单挡住
  （只有命中白名单的视频会被搬进库），不做下载前过滤。
- 落地目录：``sakuramedia_downloads/<完整 canonical info_hash>/``（库外缓冲区，与库管理目录
  平级），cid 落进 ``DownloadTask.target_ref``；完成后由对账任务触发 cleanup-source 导入。
- 选中 cloud115 即执行到底：配额耗尽、cookies 失效等直接报错，不自动降级到其它下载器。
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import IMPORT_STATUS_PENDING
from src.lib.cloud115 import (
    Cloud115DuplicateNameError,
    Cloud115Error,
    Cloud115OfflineQuotaExceededError,
    Cloud115OfflineTaskExistsError,
    OfflineTask,
)
from src.model import DownloadClient, DownloadTask
from src.service.cloud115 import (
    CLOUD115_DOWNLOADS_ROOT_NAME,
    cloud115_client_for,
    ensure_download_root_cid,
    find_or_create_subdir,
    map_cloud115_error,
)
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.shared.common import (
    DOWNLOAD_DEAD_STATES,
    ERROR_CODE_CANDIDATE_DEAD,
    canonicalize_btih,
)


async def _create_task_dir(
    client,
    *,
    download_root_cid: str,
    info_hash: str,
) -> str:
    """在下载缓冲根下建 info_hash 专属目录，返回 cid。

    直接 mkdir 而不是先分页扫描：调用方已经确认本地没有同 info_hash 的 DownloadTask，
    而 info_hash 全局唯一，绝大多数情况下目录必然不存在——旧实现那次全量翻页
    **必然扫不中**，纯属浪费（下载根随历史任务累积，页数还会增长）。

    只有一种情况目录会已存在：上一轮在"建目录成功"和"登记 DownloadTask"之间中断，
    留下孤儿目录。此时 115 返回 errno=20004（HTTP 200 + state=false，2026-07-29 实测），
    才回退到分页定位复用它。

    按 errno 精确分派、不按"POST 失败"笼统兜底：裸 HTTP 400 是 WAF 风控的签名，
    两者混淆会把风控当成重名、继续加压。
    """
    try:
        return await client.mkdir(download_root_cid, info_hash)
    except Cloud115DuplicateNameError:
        logger.info(
            "cloud115 task dir already exists, locating it info_hash={}", info_hash
        )
        return await find_or_create_subdir(
            client, parent_cid=download_root_cid, name=info_hash
        )




async def fetch_cloud115_offline_tasks_by_hash(
    client,
    *,
    page_size: int = 50,
    max_pages: int = 20,
) -> dict[str, OfflineTask]:
    """分页拉取远端离线任务，并按 canonical BTIH 建索引。"""
    results: dict[str, OfflineTask] = {}
    page = 1
    while page <= max_pages:
        task_page = await client.list_offline_tasks(page=page, page_size=page_size)
        for item in task_page.tasks:
            try:
                info_hash = canonicalize_btih(item.info_hash)
            except ValueError:
                logger.warning("ignore cloud115 offline task with invalid info_hash={!r}", item.info_hash)
                continue
            results[info_hash] = item
        if page >= task_page.page_count or not task_page.tasks:
            break
        page += 1
    return results


def resolve_magnet_from_links(
    magnet_url: str,
    torrent_url: str,
    *,
    http_client: httpx.Client | None = None,
) -> tuple[str, str]:
    """把候选链接统一成磁力，返回 (magnet, info_hash)。

    磁力优先；只有 .torrent 地址时现拉字节并用 libtorrent 解 info_hash 再拼标准磁力。
    与 qb 的 add_candidate 一致按内容而非字段名分流（索引器会把磁力塞进 torrent_url 字段）。
    """
    magnet_link = ""
    torrent_file_url = ""
    for link in (magnet_url, torrent_url):
        link = (link or "").strip()
        if not link:
            continue
        if QBittorrentClient._is_magnet(link):
            magnet_link = magnet_link or QBittorrentClient._normalize_magnet(link)
        elif not torrent_file_url:
            torrent_file_url = link

    try:
        if magnet_link:
            raw_hash = QBittorrentClient.parse_hash_from_magnet(magnet_link)
            info_hash = canonicalize_btih(raw_hash)
            return magnet_link, info_hash
        if torrent_file_url:
            client = http_client or httpx.Client(timeout=120.0, follow_redirects=True, trust_env=False)
            try:
                response = client.get(torrent_file_url)
                response.raise_for_status()
                info_hash = QBittorrentClient.parse_hash_from_torrent(response.content)
            finally:
                if http_client is None:
                    client.close()
            info_hash = canonicalize_btih(info_hash)
            return f"magnet:?xt=urn:btih:{info_hash}", info_hash
    except (QBittorrentClientError, ValueError) as exc:
        raise ApiError(
            422,
            "invalid_download_request_candidate",
            "候选链接无法解析为磁力",
            {"detail": str(exc)},
        ) from exc
    except httpx.HTTPError as exc:
        raise ApiError(
            502,
            "download_request_torrent_fetch_failed",
            "拉取 .torrent 文件失败，无法转换为磁力",
            {"detail": str(exc)},
        ) from exc
    raise ApiError(
        422,
        "invalid_download_request_candidate",
        "candidate must provide magnet_url or torrent_url",
    )


class Cloud115OfflineDownloadService:
    def __init__(self, *, http_client: httpx.Client | None = None):
        self.http_client = http_client

    def submit_candidate(
        self,
        download_client: DownloadClient,
        *,
        movie_number: str,
        candidate,
    ) -> tuple[DownloadTask, bool]:
        """向 115 提交离线磁力任务并登记本地 DownloadTask，返回 (task, created)。

        幂等性：任务键是 (client, info_hash)。本地已有记录直接复用；已判死的记录抛
        ``download_candidate_dead``，绝不当作可复用任务静默跳过。115 侧报"任务已存在"
        （良性重复，不扣配额）时同样落回 get_or_create 对齐远端。
        """
        magnet, info_hash = resolve_magnet_from_links(
            candidate.magnet_url,
            candidate.torrent_url,
            http_client=self.http_client,
        )

        existing = DownloadTask.get_or_none(
            (DownloadTask.client == download_client.id) & (DownloadTask.info_hash == info_hash)
        )
        if existing is not None:
            if existing.download_state in DOWNLOAD_DEAD_STATES:
                raise ApiError(
                    409,
                    ERROR_CODE_CANDIDATE_DEAD,
                    "该种子已判死，不会重复提交；如需重试请先删除原下载任务",
                    {
                        "movie_number": movie_number,
                        "info_hash": info_hash,
                        "download_task_id": existing.id,
                    },
                )
            return existing, False

        library = download_client.media_library

        async def _submit() -> str:
            async with cloud115_client_for(library) as client:
                download_root_cid = await ensure_download_root_cid(library, client)
                task_dir_cid = await _create_task_dir(
                    client, download_root_cid=download_root_cid, info_hash=info_hash
                )
                try:
                    results = await client.add_offline_urls([magnet], save_dir_id=task_dir_cid)
                except Cloud115OfflineTaskExistsError:
                    # 远端重复任务可能位于用户目录，必须采用真实目录并确认属于当前受管根。
                    try:
                        remote_tasks = await fetch_cloud115_offline_tasks_by_hash(client)
                        remote_task = remote_tasks.get(info_hash)
                        directory = (
                            await client.dir_info(remote_task.save_dir_id)
                            if remote_task is not None and remote_task.save_dir_id
                            else None
                        )
                    except Cloud115Error as exc:
                        raise ApiError(
                            409,
                            "cloud115_offline_task_exists_unmanaged",
                            "115 已存在同 hash 离线任务，但无法可靠定位其保存目录",
                            {"info_hash": info_hash},
                        ) from exc
                    if remote_task is None or not remote_task.save_dir_id:
                        raise ApiError(
                            409,
                            "cloud115_offline_task_exists_unmanaged",
                            "115 已存在同 hash 离线任务，但无法可靠定位其保存目录",
                            {"info_hash": info_hash},
                        )
                    ancestor_ids = {item.file_id for item in directory.paths}
                    if (
                        remote_task.save_dir_id == download_root_cid
                        or download_root_cid not in ancestor_ids
                    ):
                        raise ApiError(
                            409,
                            "cloud115_offline_task_exists_unmanaged",
                            "115 已存在同 hash 离线任务，但它不在当前媒体库的受管下载目录中",
                            {"info_hash": info_hash},
                        )
                    logger.info(
                        "reuse managed cloud115 offline task info_hash={} client_id={} cid={}",
                        info_hash, download_client.id, remote_task.save_dir_id,
                    )
                    return remote_task.save_dir_id

                if len(results) != 1:
                    raise ApiError(
                        502,
                        "cloud115_offline_submit_invalid_response",
                        "115 单项离线提交未返回唯一结果",
                        {"info_hash": info_hash, "result_count": len(results)},
                    )
                try:
                    response_hash = canonicalize_btih(results[0].info_hash)
                except ValueError as exc:
                    raise ApiError(
                        502,
                        "cloud115_offline_submit_invalid_response",
                        "115 单项离线提交未返回有效 hash",
                        {"info_hash": info_hash},
                    ) from exc
                if response_hash != info_hash:
                    raise ApiError(
                        502,
                        "cloud115_offline_submit_hash_mismatch",
                        "115 单项离线提交返回的 hash 与请求不一致",
                        {"expected_hash": info_hash, "actual_hash": response_hash},
                    )
                return task_dir_cid

        try:
            task_dir_cid = asyncio.run(_submit())
        except Cloud115OfflineQuotaExceededError as exc:
            # 用户明确不降级：配额耗尽直接报错，由用户改用其它下载器或等配额刷新。
            raise ApiError(
                409,
                "cloud115_offline_quota_exceeded",
                "115 离线下载月配额已用尽",
                {"detail": str(exc)},
            ) from exc
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc

        task, created = DownloadTask.get_or_create(
            client=download_client,
            info_hash=info_hash,
            defaults={
                "movie": movie_number,
                "name": candidate.title,
                # save_path 仅作展示；结构化定位靠 target_ref.cid（对账 / 触发导入用）。
                "save_path": f"{CLOUD115_DOWNLOADS_ROOT_NAME}/{info_hash}",
                "target_ref": {"cid": task_dir_cid},
                "progress": 0.0,
                "download_state": "queued",
                "import_status": IMPORT_STATUS_PENDING,
            },
        )
        return task, created
