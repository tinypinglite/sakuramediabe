from pathlib import Path
from typing import Dict
from uuid import uuid4

from loguru import logger

from src.config.config import settings
from src.model import DownloadClient
from src.service.transfers.qbittorrent_client import QBittorrentClient

# 打到待删除小文件上的标记前缀；物理删除阶段按文件名是否含该前缀来识别。
# 用项目专属前缀降低误删风险——避免误删恰好含通用词（如 "need_delete"）的真实文件。
NEED_DELETE_MARKER = "sakuramedia_need_delete"


class DownloadSmallFileCleanupService:
    """清理下载中种子里的小文件。

    针对带 sakuramedia 标签、仍在下载的种子，把小于阈值的文件设为不下载并打上待删除标记，
    再物理删除下载暂存目录里这些被标记的残留文件，避免种子夹带的 sample / 小垃圾文件拖住整个任务。

    没有下载中种子时不会遍历下载目录：标记文件只可能由下载中种子产生，空扫除了唤醒硬盘没有任何产出。
    """

    def __init__(self, qbittorrent_client_cls=QBittorrentClient):
        self.qbittorrent_client_cls = qbittorrent_client_cls

    def cleanup_small_files(self) -> Dict[str, int]:
        threshold_bytes = settings.downloads.small_file_cleanup_threshold_mb * 1024 * 1024
        total_clients = 0
        scanned_torrents = 0
        deselected_files = 0
        deleted_files = 0
        failed_count = 0

        for client in DownloadClient.select().order_by(DownloadClient.id.asc()):
            total_clients += 1
            try:
                qb_client = self.qbittorrent_client_cls.from_download_client(client)
                # 只处理本系统添加（带 sakuramedia 标签）的种子，手动加入 qb 的种子不受影响。
                torrents = qb_client.list_torrents(client_id=client.id)
                client_scanned_torrents = 0
                for torrent in torrents:
                    # 已完成的种子无需再清理小文件。
                    if torrent.get("progress", 0.0) >= 1.0:
                        continue
                    client_scanned_torrents += 1
                    deselected_files += self._clean_torrent(
                        qb_client, torrent["info_hash"], threshold_bytes
                    )
                scanned_torrents += client_scanned_torrents
                # 待删除标记只可能由下载中种子产生；没有下载中种子时跳过物理清理，
                # 避免高频任务空扫下载目录、无谓唤醒硬盘（下载盘常与媒体盘同盘）。
                if client_scanned_torrents:
                    # 每个 client 处理完后，物理删除其下载暂存目录里被标记的小文件。
                    deleted_files += self._delete_marked_files(client.local_root_path)
            except Exception as exc:
                failed_count += 1
                logger.exception(
                    "Download small file cleanup failed for client_id={} detail={}",
                    client.id,
                    exc,
                )

        return {
            "total_clients": total_clients,
            "scanned_torrents": scanned_torrents,
            "deselected_files": deselected_files,
            "deleted_files": deleted_files,
            "failed_count": failed_count,
        }

    def _clean_torrent(self, qb_client, info_hash: str, threshold_bytes: int) -> int:
        # 筛出小于阈值且尚未被设为不下载的文件；priority==0 的已处理过，跳过以保证幂等。
        files = qb_client.list_torrent_files(info_hash)
        small_file_indexes = [
            file["index"]
            for file in files
            if file["size"] < threshold_bytes and file["priority"] != 0
        ]
        if not small_file_indexes:
            return 0
        qb_client.set_files_not_download(info_hash, small_file_indexes)
        # 重命名仅作待删除标记：qBittorrent 不会因 priority=0 或重命名删除已落盘的字节，
        # 空间靠 _delete_marked_files 物理删除回收。
        for file_index in small_file_indexes:
            qb_client.rename_torrent_file(
                info_hash, file_index, f"{NEED_DELETE_MARKER}_{uuid4().hex}"
            )
        return len(small_file_indexes)

    @staticmethod
    def _delete_marked_files(local_root_path: str) -> int:
        root = Path(local_root_path)
        if not root.exists():
            return 0
        deleted = 0
        for path in root.rglob("*"):
            if path.is_file() and NEED_DELETE_MARKER in path.name:
                try:
                    path.unlink()
                    deleted += 1
                except OSError as exc:
                    logger.warning("Failed to delete marked file {} detail={}", path, exc)
        return deleted
