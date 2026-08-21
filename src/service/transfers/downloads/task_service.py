from pathlib import Path

from loguru import logger
from peewee import JOIN

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
)
from src.common.service_helpers import validate_page
from src.model import DownloadTask, Image, Movie
from src.model.enums import DownloadClientKind
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadTaskActionResponse,
    DownloadTaskFileResource,
    DownloadTaskFilesResponse,
    DownloadTaskImportResponse,
    DownloadTaskResource,
)
from src.schema.transfers.media_import import ImportRequest
from src.service.transfers.downloads.clients.qbittorrent import (
    QBittorrentClient,
    QBittorrentClientError,
    QBittorrentTorrentNotFoundError,
    QBittorrentTorrentNotManagedError,
)
from src.service.transfers.downloads.common import (
    ALLOWED_DOWNLOAD_STATES,
    build_task_movie_filter,
    is_download_complete,
    normalize_state_filters,
    require_client,
    require_task,
    resolve_task_sort,
)
from src.service.transfers.shared.common import canonicalize_btih
from src.service.transfers.shared.import_task_service import ImportTaskService


class DownloadTaskService:
    DEFAULT_IMPORTABLE_STATUSES = {IMPORT_STATUS_PENDING, IMPORT_STATUS_FAILED, IMPORT_STATUS_SKIPPED}

    @classmethod
    def list_tasks(
        cls,
        *,
        page: int = 1,
        page_size: int = 20,
        client_id: int | None = None,
        movie_number: str | None = None,
        download_state: list[str] | None = None,
        sort: str | None = None,
    ) -> PageResponse[DownloadTaskResource]:
        validate_page(page, page_size, error_code="invalid_download_task_filter")
        query = DownloadTask.select()
        if client_id is not None:
            require_client(client_id)
            query = query.where(DownloadTask.client == client_id)
        if movie_number and movie_number.strip():
            query = query.where(build_task_movie_filter(movie_number))
        normalized_states = normalize_state_filters(
            download_state,
            field_name="download_state",
            allowed_values=ALLOWED_DOWNLOAD_STATES,
        )
        if normalized_states is not None:
            query = query.where(
                DownloadTask.download_state.in_(tuple(sorted(normalized_states)))
            )

        total = query.count()
        tasks = list(
            query.order_by(*resolve_task_sort(sort)).paginate(page, page_size)
        )
        movies_by_number = cls._load_movies_for_tasks(tasks)
        return PageResponse[DownloadTaskResource](
            items=DownloadTaskResource.from_models(tasks, movies_by_number=movies_by_number),
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def list_task_files(
        cls,
        task_id: int,
        *,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> DownloadTaskFilesResponse:
        """按任务实时拉取文件列表：qB 走 Web API，cloud115 走 SDK 递归列目录。

        刻意不做提交时快照落库——列表是查看型数据，实时拉取永远反映远端现状，
        也不需要为新增列做迁移。任务在远端已不存在时按对应客户端语义报 404。
        """
        task = require_task(task_id)
        if task.client.kind == DownloadClientKind.CLOUD115.value:
            return cls._list_cloud115_task_files(task)
        return cls._list_qbittorrent_task_files(
            task, qbittorrent_client_cls=qbittorrent_client_cls
        )

    @classmethod
    def _list_qbittorrent_task_files(
        cls,
        task,
        *,
        qbittorrent_client_cls,
    ) -> DownloadTaskFilesResponse:
        qb_client = qbittorrent_client_cls.from_download_client(task.client)
        try:
            files = qb_client.list_managed_torrent_files(
                task.info_hash,
                client_id=task.client_id,
            )
        except QBittorrentTorrentNotFoundError as exc:
            # 本地行还在但 qB 侧已被人工清理（幽灵任务）：给 404 让前端说清楚，
            # 不要包装成"读取失败"这类 5xx，用户会误以为是暂时性故障。
            raise ApiError(
                404,
                "download_task_remote_missing",
                "qBittorrent 中已找不到该下载任务",
                {"task_id": task.id},
            ) from exc
        except QBittorrentTorrentNotManagedError as exc:
            raise ApiError(
                409,
                "download_task_not_managed",
                "qBittorrent torrent is not managed by this download client",
                {"task_id": task.id},
            ) from exc
        except QBittorrentClientError as exc:
            raise ApiError(
                502,
                "download_task_files_failed",
                "读取 qBittorrent 文件列表失败",
                {"task_id": task.id, "detail": str(exc)},
            ) from exc
        return DownloadTaskFilesResponse(
            task_id=task.id,
            client_kind=task.client.kind,
            files=[
                DownloadTaskFileResource(
                    name=file.get("name") or "",
                    size=int(file.get("size") or 0),
                )
                for file in files
            ],
        )

    @classmethod
    def _list_cloud115_task_files(cls, task) -> DownloadTaskFilesResponse:
        target_ref = task.target_ref or {}
        source_cid = target_ref.get("cid")
        if not source_cid:
            # 与 115 对账链路同一判例：缺 cid 属数据缺陷，重试不可恢复，直接明确报错。
            raise ApiError(
                422,
                "cloud115_download_task_missing_target_ref",
                "cloud115 下载任务缺少 target_ref.cid，无法读取文件列表",
                {"task_id": task.id},
            )

        import asyncio

        from src.lib.cloud115 import Cloud115Error, Cloud115NotFoundError
        from src.service.cloud115 import cloud115_client_for, map_cloud115_error
        from src.service.transfers.cloud115.importer.common import (
            collect_cloud115_source_files,
        )

        async def _fetch():
            async with cloud115_client_for(task.client.media_library) as sdk_client:
                return await collect_cloud115_source_files(
                    sdk_client,
                    source_cid,
                    # 全量枚举：文件列表要展示所有条目，不做后缀预筛。
                    needs_rel_path=lambda entry: True,
                )

        try:
            entries, rel_dirs = asyncio.run(_fetch())
        except Cloud115NotFoundError as exc:
            # 115 cleanup-source 成功后源目录会被移入回收站；这不是可重试的上游故障，
            # 明确告诉调用方源目录已不可用，避免把已清理任务包装成 502。
            raise ApiError(
                404,
                "cloud115_download_task_source_unavailable",
                "115 下载任务的源目录已不存在，可能已被清理或手动删除，无法读取文件列表",
                {"task_id": task.id},
            ) from exc
        except Cloud115Error as exc:
            raise map_cloud115_error(exc) from exc
        except Exception as exc:
            raise ApiError(
                502,
                "cloud115_download_task_files_failed",
                "读取 115 文件列表失败",
                {"task_id": task.id, "detail": str(exc)},
            ) from exc

        files: list[DownloadTaskFileResource] = []
        for entry in entries:
            if entry.is_dir:
                continue
            rel_dir_parts = rel_dirs.get(entry.parent_id) or ()
            files.append(
                DownloadTaskFileResource(
                    name=entry.name,
                    size=entry.size,
                    path="/".join([*rel_dir_parts, entry.name]),
                )
            )
        files.sort(key=lambda item: item.name.lower())
        return DownloadTaskFilesResponse(
            task_id=task.id,
            client_kind=task.client.kind,
            files=files,
        )

    @staticmethod
    def _load_movies_for_tasks(tasks) -> dict[str, Movie]:
        """按番号批量 JOIN 影片元数据（标题 + 封面），保证列表接口只做一趟 JOIN 而非 N+1。"""
        numbers = list({task.movie for task in tasks if task.movie})
        if not numbers:
            return {}
        movies = (
            Movie.select(Movie, Image)
            .join(Image, JOIN.LEFT_OUTER, on=(Movie.cover_image == Image.id))
            .where(Movie.movie_number.in_(numbers))
        )
        return {movie.movie_number: movie for movie in movies}

    @classmethod
    def pause_task(
        cls,
        task_id: int,
        *,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> DownloadTaskActionResponse:
        task = require_task(task_id)
        cls._operate_remote_task(
            task,
            action="pause",
            qbittorrent_client_cls=qbittorrent_client_cls,
        )
        return DownloadTaskActionResponse(task_id=task.id, action="pause")

    @classmethod
    def resume_task(
        cls,
        task_id: int,
        *,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> DownloadTaskActionResponse:
        task = require_task(task_id)
        cls._operate_remote_task(
            task,
            action="resume",
            qbittorrent_client_cls=qbittorrent_client_cls,
        )
        return DownloadTaskActionResponse(task_id=task.id, action="resume")

    @classmethod
    def delete_task(
        cls,
        task_id: int,
        *,
        delete_files: bool,
        qbittorrent_client_cls=QBittorrentClient,
    ) -> dict:
        task = require_task(task_id)
        if task.import_status == IMPORT_STATUS_RUNNING:
            raise ApiError(
                409,
                "download_task_import_running",
                "Cannot delete a download task while importing media",
                {"task_id": task.id},
            )

        if task.client.kind == DownloadClientKind.CLOUD115.value:
            return cls._delete_cloud115_task(task, delete_files=delete_files)

        qb_client = qbittorrent_client_cls.from_download_client(task.client)
        try:
            # 远端任务已被人工清理时，本地镜像仍可安全删除；若远端存在则客户端层会再次验标签。
            qb_client.delete_torrent(
                task.info_hash,
                client_id=task.client_id,
                delete_files=delete_files,
            )
        except QBittorrentTorrentNotManagedError as exc:
            raise ApiError(
                409,
                "download_task_not_managed",
                "qBittorrent torrent is not managed by this download client",
                {"task_id": task.id},
            ) from exc
        except QBittorrentClientError as exc:
            logger.warning("Delete qBittorrent task failed task_id={} detail={}", task.id, exc)
            raise ApiError(
                502,
                "download_task_delete_failed",
                "qBittorrent request failed",
                {"task_id": task.id, "detail": str(exc)},
            ) from exc

        removed = {
            "task_id": task.id,
            "client_id": task.client_id,
            "movie_number": task.movie,
            "info_hash": task.info_hash,
        }
        task.delete_instance()
        return removed

    @classmethod
    def _delete_cloud115_task(cls, task: DownloadTask, *, delete_files: bool) -> dict:
        """删除 cloud115 离线任务：先删 115 侧任务（delete_files 时连已下载文件一起删），再删本地镜像。

        abandoned 任务的语义是"115 侧保留、本地停止关注"，删除它时只清本地记录，不动远端。
        """
        import asyncio

        from src.lib.cloud115 import Cloud115Error
        from src.service.cloud115 import (
            cloud115_client_for,
            map_cloud115_error,
        )

        try:
            canonical_hash = canonicalize_btih(task.info_hash)
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_cloud115_download_task_hash",
                "cloud115 下载任务缺少有效的 canonical hash",
                {"task_id": task.id},
            ) from exc

        if task.download_state != "abandoned":
            from src.lib.cloud115 import Cloud115NotFoundError

            async def _delete_remote() -> None:
                async with cloud115_client_for(task.client.media_library) as sdk_client:
                    await sdk_client.delete_offline_tasks(
                        [canonical_hash], delete_source_files=delete_files
                    )

            try:
                asyncio.run(_delete_remote())
            except Cloud115NotFoundError:
                # 远端明确不存在（用户已在 115 App 手动删过、或上次删除请求远端成功
                # 但本地事务失败留下的悬空任务）：视为"已经删干净"，继续走本地删除，
                # 避免本地记录永久卡死无法清理。cookies 失效/限流等其它上游错误仍然
                # 保留本地记录并向上报错，以便用户重试。
                logger.info(
                    "Cloud115 offline task already gone remotely, proceeding with local delete task_id={}",
                    task.id,
                )
            except Cloud115Error as exc:
                logger.warning(
                    "Delete cloud115 offline task failed task_id={} detail={}", task.id, exc
                )
                raise map_cloud115_error(exc) from exc

        removed = {
            "task_id": task.id,
            "client_id": task.client_id,
            "movie_number": task.movie,
            "info_hash": canonical_hash,
        }
        task.delete_instance()
        return removed

    @classmethod
    def _operate_remote_task(cls, task: DownloadTask, *, action: str, qbittorrent_client_cls) -> None:
        # 115 离线是服务端下载，没有暂停/恢复原语；对 cloud115 任务明确拒绝而不是静默无效。
        if task.client.kind == DownloadClientKind.CLOUD115.value:
            raise ApiError(
                422,
                "download_task_action_unsupported",
                f"cloud115 离线任务不支持 {action}",
                {"task_id": task.id, "action": action},
            )
        qb_client = qbittorrent_client_cls.from_download_client(task.client)
        try:
            if action == "pause":
                qb_client.pause_torrent(task.info_hash, client_id=task.client_id)
            else:
                qb_client.resume_torrent(task.info_hash, client_id=task.client_id)
        except QBittorrentTorrentNotFoundError as exc:
            raise ApiError(
                409,
                "download_task_remote_missing",
                "qBittorrent torrent is no longer available",
                {"task_id": task.id},
            ) from exc
        except QBittorrentTorrentNotManagedError as exc:
            raise ApiError(
                409,
                "download_task_not_managed",
                "qBittorrent torrent is not managed by this download client",
                {"task_id": task.id},
            ) from exc
        except QBittorrentClientError as exc:
            logger.warning("{} qBittorrent task failed task_id={} detail={}", action, task.id, exc)
            raise ApiError(
                502,
                f"download_task_{action}_failed",
                "qBittorrent request failed",
                {"task_id": task.id, "detail": str(exc)},
            ) from exc

    @classmethod
    def trigger_import(
        cls,
        task_id: int,
        *,
        allowed_statuses: set[str] | None = None,
        trigger_type: str = "manual",
    ) -> DownloadTaskImportResponse:
        task = require_task(task_id)
        if not is_download_complete(task.download_state):
            raise ApiError(
                422,
                "invalid_download_task_import",
                "Only completed download tasks can be imported",
                {"task_id": task_id},
            )

        importable_statuses = allowed_statuses or cls.DEFAULT_IMPORTABLE_STATUSES
        if task.import_status not in importable_statuses:
            raise ApiError(
                409,
                "download_task_import_conflict",
                "Download task import is already running or completed",
                {"task_id": task_id, "import_status": task.import_status},
            )

        if task.client.kind == DownloadClientKind.CLOUD115.value:
            # cloud115 任务的导入是云端搬运（cleanup-source = move），走 cloud115 导入作业链路。
            from src.service.transfers.cloud115.offline.sync_service import (
                Cloud115OfflineSyncService,
            )

            response = Cloud115OfflineSyncService.trigger_task_import(
                task, trigger_type=trigger_type
            )
            return response

        source_path = cls._resolve_import_source_path(task.save_path)
        accepted = ImportTaskService.enqueue(
            ImportRequest(
                media_kind="jav",
                backend="local",
                library_id=task.client.media_library_id,
                source_path=str(source_path),
            ),
            trigger_type=trigger_type,
            download_task_id=task.id,
            task_name=f"下载任务导入 {task.movie or task.name}",
        )
        return DownloadTaskImportResponse(
            task_id=task.id,
            task_run_id=accepted.task_run_id,
            status="accepted",
        )

    @staticmethod
    def _resolve_import_source_path(save_path: str) -> Path:
        path = Path(save_path).expanduser().resolve()
        if path.is_dir():
            return path
        if path.is_file():
            return path
        raise ApiError(
            422,
            "invalid_download_task_import_path",
            "Download task save path is not accessible",
            {"save_path": save_path},
        )
