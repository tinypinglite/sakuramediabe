from datetime import timedelta

from loguru import logger

from src.config.config import settings
from src.common.media_import_status import IMPORT_STATUS_RUNNING
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask
from src.model.enums import DownloadClientKind
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.downloads.common import (
    DOWNLOAD_ACTIVE_DOWNLOAD_STATES,
    DOWNLOAD_STALLED_DEAD_STATE,
    map_download_state,
)
from src.service.transfers.shared.common import DOWNLOAD_DEAD_STATES


class QBStalledCleanupService:
    """清理长期停滞 / 龟速下载的 qB 任务。

    判定基准是 DownloadTask.download_started_at（对账维护的"进入活跃下载态时刻"），
    而非 qB 的 added_on——qB 接口没有"开始下载时刻"字段，直接用添加时刻会把排队时长
    算进"下载时长"，2000 部订阅 + 少量并发时队尾种子排队几天必然轮不上，会被成批误删。

    命中后：qB 删种并连带删除已下载文件，本地行落 stalled_dead（选种黑名单台账）——
    同 info_hash 不再被自动下载重新提交，影片会换其他候选继续。
    """

    def __init__(self, qbittorrent_client_cls=QBittorrentClient):
        self.qbittorrent_client_cls = qbittorrent_client_cls

    def cleanup_stalled_tasks(self) -> dict[str, int]:
        if not settings.downloads.qbittorrent_stalled_cleanup_enabled:
            return {
                "total_clients": 0,
                "scanned_torrents": 0,
                "cleaned_count": 0,
                "failed_count": 0,
            }
        hours = settings.downloads.qbittorrent_stalled_cleanup_hours
        total_clients = 0
        scanned_torrents = 0
        cleaned_count = 0
        failed_count = 0

        for client in (
            DownloadClient.select()
            .where(DownloadClient.kind == DownloadClientKind.QBITTORRENT.value)
            .order_by(DownloadClient.id.asc())
        ):
            total_clients += 1
            try:
                qb_client = self.qbittorrent_client_cls.from_download_client(client)
                # 只处理本系统添加（带 sakuramedia 标签）的种子，手动加入 qb 的种子不受影响。
                torrents = qb_client.list_torrents(client_id=client.id)
            except QBittorrentClientError as exc:
                logger.warning(
                    "qb stalled cleanup failed: client_id={} error={}",
                    client.id,
                    exc,
                )
                failed_count += 1
                continue

            for torrent in torrents:
                # 清理只看归一化状态：stalledDL（等待资源）/ downloading（龟速）命中；
                # queuedDL（排队）与 pausedDL/stoppedDL（用户意图）永不清理。
                state = map_download_state(torrent.get("state", ""))
                if state not in DOWNLOAD_ACTIVE_DOWNLOAD_STATES:
                    continue
                if torrent.get("progress", 0.0) >= 1.0:
                    continue
                scanned_torrents += 1
                result = self._clean_torrent(
                    qb_client,
                    client.id,
                    torrent["info_hash"],
                    hours,
                    torrent.get("last_activity"),
                )
                if result == "cleaned":
                    cleaned_count += 1
                elif result == "failed":
                    failed_count += 1

        logger.info(
            "qb stalled cleanup finished: total_clients={} scanned_torrents={} "
            "cleaned_count={} failed_count={}",
            total_clients,
            scanned_torrents,
            cleaned_count,
            failed_count,
        )
        return {
            "total_clients": total_clients,
            "scanned_torrents": scanned_torrents,
            "cleaned_count": cleaned_count,
            "failed_count": failed_count,
        }

    def _clean_torrent(
        self,
        qb_client: QBittorrentClient,
        client_id: int,
        info_hash: str,
        hours: int,
        last_activity: int | None,
    ) -> str:
        """单种子清理：超时则删种 + 删文件 + 本地行落死态拉黑。

        计时优先用对账维护的 download_started_at（排队时长不计）；started_at 为空时回退到
        qB 的 last_activity——典型是存量部署时已在 qB 躺超 7 天、首轮对账即被 7 天判死落
        stalled_dead 的种子（判死时对账清空了 started_at），不回退的话清理对它永久够不着，
        种子与已下载文件会一直留在 qB。

        返回 "cleaned"（已清理）/ "skipped"（未命中，不计失败）/ "failed"（删除报错，下次再试）。
        """
        task = DownloadTask.get_or_none(
            DownloadTask.client_id == client_id,
            DownloadTask.info_hash == info_hash,
        )
        if task is None:
            # 无本地行（非系统提交的种子）不清理。
            return "skipped"
        if task.import_status == IMPORT_STATUS_RUNNING:
            # 导入进行中绝不删文件（与 _prune_ghost_tasks 的导入白名单同一防御）。
            return "skipped"
        now = utc_now_for_db()
        started_at = task.download_started_at
        if started_at is not None:
            if now - started_at < timedelta(hours=hours):
                return "skipped"
        else:
            # 兜底计时：qB 的 last_activity 是"最后收发 chunk 的时刻"，从未活动的种子 qB
            # 返回添加时刻。仅在该种子被 7 天判死清空 started_at 后才会走到这里——
            # 正常排队→轮上→下载的种子 started_at 早已由对账写入，不会用 added_on 误伤。
            if not isinstance(last_activity, int) or isinstance(last_activity, bool):
                return "skipped"
            now_ts = int(now.timestamp())
            if last_activity <= 0 or last_activity > now_ts:
                return "skipped"
            if now_ts - last_activity < hours * 3600:
                return "skipped"

        try:
            removed = qb_client.delete_torrent(
                info_hash,
                client_id=client_id,
                delete_files=True,
            )
        except QBittorrentClientError as exc:
            logger.warning(
                "qb stalled cleanup delete failed: client_id={} info_hash={} error={}",
                client_id,
                info_hash,
                exc,
            )
            return "failed"

        # 落死态作为选种黑名单台账：qB 侧已无此种子，对账不会再把状态流回；
        # _prune_ghost_tasks 对死态行豁免，行保留在黑名单里。
        # 原子 UPDATE 带死态守卫 + rowcount 校验：delete_torrent 是秒级 HTTP 调用，期间对账
        # 可能已把行 prune 掉（当时行还是活跃态，豁免不生效）——守卫保证绝不把别处新写回
        # 的死态覆写掉，0 行命中时行已被删，重插死态行保住黑名单。
        # 同时清空 started_at：恢复"离开活跃态即清空"不变量，用户手动重加同一 hash 后
        # 对账会重新起算 24h 宽限，而不是拿旧时间戳秒删。
        updated = (
            DownloadTask.update(
                download_state=DOWNLOAD_STALLED_DEAD_STATE,
                download_started_at=None,
            )
            .where(
                DownloadTask.id == task.id,
                DownloadTask.download_state.not_in(tuple(sorted(DOWNLOAD_DEAD_STATES))),
            )
            .execute()
        )
        if updated == 0:
            if not DownloadTask.select().where(DownloadTask.id == task.id).exists():
                # 行已被对账 prune（黑名单丢失）：按任务幂等键重插死态行，保住黑名单。
                DownloadTask.create(
                    client_id=client_id,
                    info_hash=info_hash,
                    movie=task.movie,
                    name=task.name,
                    save_path=task.save_path,
                    progress=task.progress,
                    download_state=DOWNLOAD_STALLED_DEAD_STATE,
                    import_status=task.import_status,
                )
        logger.info(
            "qb stalled cleanup removed stalled torrent: client_id={} info_hash={} "
            "movie={} name={} remote_removed={}",
            client_id,
            info_hash,
            task.movie,
            task.name,
            removed,
        )
        return "cleaned"
