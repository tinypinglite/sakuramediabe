
from loguru import logger

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
)
from src.common.movie_numbers import parse_movie_number_from_text
from src.common.runtime_time import utc_now_for_db
from src.model import DownloadClient, DownloadTask
from src.model.enums import DownloadClientKind
from src.schema.transfers.downloads import DownloadClientSyncResponse
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
)
from src.service.transfers.downloads.common import (
    DOWNLOAD_ACTIVE_DOWNLOAD_STATES,
    DOWNLOAD_COMPLETE_STATES,
    map_remote_path,
    require_client,
    resolve_qbittorrent_download_state,
)
from src.service.transfers.downloads.task_service import DownloadTaskService
from src.service.transfers.shared.common import download_task_dead_expression
from src.service.transfers.shared.import_task_service import ImportTaskService


class DownloadSyncService:
    def __init__(self, qbittorrent_client_cls=QBittorrentClient):
        self.qbittorrent_client_cls = qbittorrent_client_cls

    def sync_client(self, client_id: int) -> DownloadClientSyncResponse:
        client = require_client(client_id)
        qb_client = self.qbittorrent_client_cls.from_download_client(client)
        try:
            remote_tasks = qb_client.list_torrents(client_id=client.id)
        except QBittorrentClientError as exc:
            # qBittorrent 客户端层本身不记日志，这里转 502 前先记，避免真实报错只存在于响应体
            logger.warning(
                "download task sync failed: client_id={} error={}",
                client_id,
                exc,
            )
            raise ApiError(
                502,
                "download_task_sync_failed",
                "qBittorrent request failed",
                {"detail": str(exc), "client_id": client_id},
            ) from exc

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        remote_hashes: set[str] = set()
        now = utc_now_for_db()
        for remote_task in remote_tasks:
            # 判死收口在对账：stalledDL 且 qB 的 last_activity 超窗 -> stalled_dead。
            normalized_state = resolve_qbittorrent_download_state(
                remote_task.get("state", ""), remote_task.get("last_activity")
            )
            movie_number = parse_movie_number_from_text(
                f"{remote_task.get('name', '')} {remote_task.get('save_path', '')}"
            ) or None
            mapped_path = map_remote_path(client, remote_task.get("save_path") or client.client_save_path)
            remote_hashes.add(remote_task["info_hash"])
            task, created = DownloadTask.get_or_create(
                client=client,
                info_hash=remote_task["info_hash"],
                defaults={
                    "movie": movie_number,
                    "name": remote_task.get("name", ""),
                    "save_path": mapped_path,
                    "progress": remote_task.get("progress", 0.0),
                    "download_state": normalized_state,
                    "import_status": IMPORT_STATUS_PENDING,
                    # 重建的行若已处于活跃下载态，开始时刻按当前对账时刻起算。
                    "download_started_at": (
                        now if normalized_state in DOWNLOAD_ACTIVE_DOWNLOAD_STATES else None
                    ),
                },
            )
            if created:
                created_count += 1
                continue

            changed_fields = []
            # 只填空不覆写：提交时写入的 movie_number 是 Movie 规范列的拷贝（权威），
            # 这里 parse 出的是猜测（正则可能转写分隔符），拿猜测覆写权威值会打断与 Movie 的 JOIN。
            # parse 的唯一正当用途是本地行丢失后从 qB 侧重建（上面 get_or_create 的 defaults）。
            if movie_number and task.movie is None:
                task.movie = movie_number
                changed_fields.append(DownloadTask.movie)
            if task.name != remote_task.get("name", ""):
                task.name = remote_task.get("name", "")
                changed_fields.append(DownloadTask.name)
            if task.save_path != mapped_path:
                task.save_path = mapped_path
                changed_fields.append(DownloadTask.save_path)
            if task.progress != remote_task.get("progress", 0.0):
                task.progress = remote_task.get("progress", 0.0)
                changed_fields.append(DownloadTask.progress)
            if task.download_state != normalized_state:
                task.download_state = normalized_state
                changed_fields.append(DownloadTask.download_state)
            # 维护"进入活跃下载态的时刻"：进入 stalled/downloading 且为空时起算，
            # 离开（暂停/完成/做种/排队/失败等）即清空——排队时长不计入下载时长。
            if normalized_state in DOWNLOAD_ACTIVE_DOWNLOAD_STATES:
                if task.download_started_at is None:
                    task.download_started_at = now
                    changed_fields.append(DownloadTask.download_started_at)
            elif task.download_started_at is not None:
                task.download_started_at = None
                changed_fields.append(DownloadTask.download_started_at)
            if changed_fields:
                # 只保存本轮真正变化的字段，避免并发采样已推进 progress/state 后被旧对账实例覆写。
                task.save(only=changed_fields)
                updated_count += 1
            else:
                unchanged_count += 1

        removed_count = self._prune_ghost_tasks(client.id, remote_hashes)

        return DownloadClientSyncResponse(
            client_id=client.id,
            scanned_count=len(remote_tasks),
            created_count=created_count,
            updated_count=updated_count,
            unchanged_count=unchanged_count,
            removed_count=removed_count,
        )

    @staticmethod
    def _prune_ghost_tasks(client_id: int, remote_hashes: set[str]) -> int:
        """把 qB 侧已消失的下载任务从本地清掉，作为 sync 的反向对账。

        白名单：import_status == running，表示统一 TaskRun 正在排队或执行。

        保底：qB 返回空列表时直接跳过，避免 tag 被误清 / 异常空返回一次抹掉所有本地记录。
        用单条 DELETE 让白名单条件在 DB 层原子求值，关掉"先 SELECT ghost_ids、再按 id
        DELETE"中间被并发的 trigger_import 插队的 race——那种场景下正在启动导入的任务会被
        误删，runner 后续 require_task 会抛错。

        """
        if not remote_hashes:
            logger.warning(
                "Skip ghost download task prune: qBittorrent returned empty list "
                "for client_id={} — check tag / auth state before manual cleanup",
                client_id,
            )
            return 0

        removed_count = DownloadTask.delete().where(
            (DownloadTask.client == client_id)
            & DownloadTask.info_hash.not_in(list(remote_hashes))
            # 死态行豁免：failed / abandoned / stalled_dead 是选种黑名单台账，qB 侧删种后
            # 不能顺手抹掉——否则停滞清理删掉的种子下轮对账就"消失"，黑名单失效，
            # 自动下载第二天又把同一死种拉回来。
            & ~download_task_dead_expression()
            & (DownloadTask.import_status != IMPORT_STATUS_RUNNING)
        ).execute()
        if removed_count:
            logger.info(
                "Pruned {} ghost download tasks for client_id={}",
                removed_count,
                client_id,
            )
        return removed_count

    def sync_all_clients(self) -> dict[str, int]:
        total_scanned = 0
        total_created = 0
        total_updated = 0
        total_unchanged = 0
        total_removed = 0
        total_clients = 0
        failed_client_ids: list[int] = []
        # 本服务是 qb 专属对账；cloud115 离线任务由 Cloud115OfflineSyncService 独立对账。
        for client in (
            DownloadClient.select()
            .where(DownloadClient.kind == DownloadClientKind.QBITTORRENT.value)
            .order_by(DownloadClient.id.asc())
        ):
            total_clients += 1
            try:
                summary = self.sync_client(client.id)
            except Exception as exc:
                failed_client_ids.append(client.id)
                logger.exception(
                    "Download task sync failed for client_id={} detail={}",
                    client.id,
                    exc,
                )
                continue
            total_scanned += summary.scanned_count
            total_created += summary.created_count
            total_updated += summary.updated_count
            total_unchanged += summary.unchanged_count
            total_removed += summary.removed_count
        return {
            "total_clients": total_clients,
            "scanned_count": total_scanned,
            "created_count": total_created,
            "updated_count": total_updated,
            "unchanged_count": total_unchanged,
            "removed_count": total_removed,
            "failed_count": len(failed_client_ids),
            "failed_client_ids": failed_client_ids,
        }

    def enqueue_auto_imports(self) -> dict[str, int]:
        recovered_count = self._recover_orphaned_imports()
        queued_count = 0
        # 只排 qb 任务：cloud115 任务的落地是云端 cid，本地路径导入语义不适用，
        # 其完成后导入由 Cloud115OfflineSyncService 在对账时直接触发。
        qb_client_ids = DownloadClient.select(DownloadClient.id).where(
            DownloadClient.kind == DownloadClientKind.QBITTORRENT.value
        )
        for task in DownloadTask.select().where(
            DownloadTask.download_state.in_(DOWNLOAD_COMPLETE_STATES)
            & (DownloadTask.import_status == IMPORT_STATUS_PENDING)
            & DownloadTask.client.in_(qb_client_ids)
        ):
            try:
                DownloadTaskService.trigger_import(
                    task.id,
                    allowed_statuses={IMPORT_STATUS_PENDING},
                    trigger_type="internal",
                )
                queued_count += 1
            except ApiError as exc:
                logger.warning(
                    "Skip auto import for task_id={} code={} detail={}",
                    task.id,
                    exc.code,
                    exc.details,
                )
        return {"queued_count": queued_count, "recovered_count": recovered_count}

    def recover_orphaned_imports_only(self) -> dict[str, int]:
        # 启动恢复场景只做状态回收，不触发新的自动导入排队。
        recovered_count = self._recover_orphaned_imports()
        return {"recovered_count": recovered_count}

    @staticmethod
    def _recover_orphaned_imports() -> int:
        return ImportTaskService.recover_interrupted_downloads()
