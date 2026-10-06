"""Download task ledger and host-side import handoff."""

from __future__ import annotations

from src.api.exception.errors import ApiError
from src.common.media_import_status import (
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_PENDING,
    IMPORT_STATUS_RUNNING,
    IMPORT_STATUS_SKIPPED,
)
from src.common.service_helpers import validate_page, with_movie_card_relations
from src.model import (
    DownloadResourceBlacklist,
    DownloadSubmissionRecord,
    DownloadTask,
    Movie,
)
from src.model.base import get_database
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ProviderOperationError,
    ProviderUnavailableError,
)
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadTaskBatchImportResponse,
    DownloadTaskFileResource,
    DownloadTaskImportResponse,
    DownloadTaskResource,
)
from src.schema.transfers.media_import import ImportRequest
from src.service.system import ActivityService
from src.service.transfers.downloads.common import (
    build_task_movie_filter,
    download_provider,
    is_download_complete,
    library_handle_for,
    normalize_state_filters,
    require_library,
    require_task,
    resolve_task_sort,
)
from src.service.transfers.downloads.resource_hash import canonical_info_hash
from src.service.transfers.shared.import_task_service import ImportTaskService
from src.service.transfers.shared.write_mutex import library_import_mutex_key


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
        state: list[str] | None = None,
        sort: str | None = None,
    ) -> PageResponse[DownloadTaskResource]:
        validate_page(page, page_size, error_code="invalid_download_task_filter")
        query = DownloadTask.select()
        if client_id is not None:
            query = query.where(DownloadTask.client == client_id)
        if movie_number and movie_number.strip():
            query = query.where(build_task_movie_filter(movie_number))
        normalized_states = normalize_state_filters(state, field_name="state")
        if normalized_states is not None:
            query = query.where(DownloadTask.state.in_(tuple(sorted(normalized_states))))
        total = query.count()
        tasks = list(query.order_by(*resolve_task_sort(sort)).paginate(page, page_size))
        movies_by_number = cls._load_movies_for_tasks(tasks)
        return PageResponse[DownloadTaskResource](
            items=DownloadTaskResource.from_models(tasks, movies_by_number=movies_by_number),
            page=page,
            page_size=page_size,
            total=total,
        )

    @staticmethod
    def _load_movies_for_tasks(tasks) -> dict[str, Movie]:
        numbers = list({task.movie for task in tasks if task.movie})
        if not numbers:
            return {}
        movies, _thin_cover_alias = with_movie_card_relations(Movie.select(Movie))
        movies = movies.where(Movie.movie_number.in_(numbers))
        return {movie.movie_number: movie for movie in movies}

    @classmethod
    def delete_task(cls, task_id: int, *, delete_files: bool) -> dict:
        task = require_task(task_id)
        if task.import_status == IMPORT_STATUS_RUNNING:
            raise ApiError(
                409,
                "download_task_import_running",
                "Cannot delete a download task while importing media",
                {"task_id": task.id},
            )
        # 规范 hash 用于失败资源拉黑与删除墓碑匹配；remote_id 若是非 BT 的 opaque
        # 标识，拿不到规范 hash 时仅按 remote_id 匹配。失败/跳过的任务删除前必须
        # 拉黑资源，hash 无法规范化时保持原有语义：拒绝删除，不碰远端。
        record = (
            DownloadSubmissionRecord.select()
            .where(DownloadSubmissionRecord.task_id == task.id)
            .order_by(DownloadSubmissionRecord.id.desc())
            .first()
        )
        info_hash = None
        if task.import_status in {IMPORT_STATUS_FAILED, IMPORT_STATUS_SKIPPED}:
            info_hash = canonical_info_hash(record.info_hash if record else task.remote_id)
        else:
            try:
                info_hash = canonical_info_hash(
                    record.info_hash if record else task.remote_id
                )
            except ApiError:
                info_hash = None
        try:
            download_provider(task.client).delete_task(
                remote_id=task.remote_id,
                delete_files=delete_files,
            )
        except ProviderOperationError as exc:
            if exc.code != "source_not_found":
                raise cls._provider_error(exc) from exc
        removed = {
            "task_id": task.id,
            "client_id": task.client_id,
            "movie_number": task.movie,
            "remote_id": task.remote_id,
        }
        with get_database().atomic():
            if info_hash is not None and task.import_status in {
                IMPORT_STATUS_FAILED,
                IMPORT_STATUS_SKIPPED,
            }:
                DownloadResourceBlacklist.insert(info_hash=info_hash).on_conflict_ignore().execute()
            # 删除墓碑：宿主主动删除后，同步的重认领不得再把它恢复回来。
            # 匹配同一资源的所有历史提交记录，避免旧的重复提交记录绕过墓碑。
            tombstone = DownloadSubmissionRecord.remote_id == task.remote_id
            if info_hash is not None:
                tombstone = tombstone | (
                    DownloadSubmissionRecord.info_hash == info_hash
                )
            DownloadSubmissionRecord.update(state="deleted").where(
                (DownloadSubmissionRecord.client_id == task.client_id)
                & (DownloadSubmissionRecord.state == "submitted")
                & tombstone
            ).execute()
            task.delete_instance()
        return removed

    @classmethod
    def trigger_import(
        cls,
        task_id: int,
        *,
        allowed_statuses: set[str] | None = None,
        trigger_type: str = "manual",
    ) -> DownloadTaskImportResponse:
        task = require_task(task_id)
        if not is_download_complete(task.state) or task.completed_source_ref is None:
            raise ApiError(
                422,
                "invalid_download_task_import",
                "只有下载已完成且带导入来源的任务才能导入",
                {"task_id": task_id},
            )
        importable_statuses = allowed_statuses or cls.DEFAULT_IMPORTABLE_STATUSES
        if task.import_status not in importable_statuses:
            raise ApiError(
                409,
                "download_task_import_conflict",
                "该任务的导入已完成或正在进行",
                {"task_id": task_id, "import_status": task.import_status},
            )
        accepted = ImportTaskService.enqueue(
            ImportRequest(
                media_kind="jav",
                library_id=task.client.library_id,
                source_ref=task.completed_source_ref,
                source_disposition="keep",
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

    @classmethod
    def trigger_import_batch(
        cls,
        task_ids: list[int],
    ) -> DownloadTaskBatchImportResponse:
        """批量重新导入：只接受已下载完成且导入失败/跳过的任务。

        校验不通过的 id 进 skipped 清单；有效任务按媒体库分组，每个库入队一次
        批量任务运行（同库导入互斥，任一库已有在跑任务则整体 409，不部分入队）。
        """
        retryable_statuses = {IMPORT_STATUS_FAILED, IMPORT_STATUS_SKIPPED}
        normalized_ids = list(dict.fromkeys(task_ids))
        tasks = list(
            DownloadTask.select().where(DownloadTask.id.in_(normalized_ids))
        )
        tasks_by_id = {task.id: task for task in tasks}
        skipped_task_ids: list[int] = []
        retryable_tasks: list[DownloadTask] = []
        for task_id in normalized_ids:
            task = tasks_by_id.get(task_id)
            if (
                task is None
                or not is_download_complete(task.state)
                or task.completed_source_ref is None
                or task.import_status not in retryable_statuses
            ):
                skipped_task_ids.append(task_id)
                continue
            retryable_tasks.append(task)
        if not retryable_tasks:
            return DownloadTaskBatchImportResponse(
                accepted_count=0,
                skipped_task_ids=skipped_task_ids,
            )
        tasks_by_library: dict[int, list[DownloadTask]] = {}
        for task in retryable_tasks:
            tasks_by_library.setdefault(task.client.library_id, []).append(task)
        # 入队前统一预检互斥，避免跨库批量只入队一半。
        for library_id in tasks_by_library:
            library = require_library(library_id)
            if (
                ActivityService.find_task_run_by_mutex_key(
                    library_import_mutex_key(library=library)
                )
                is not None
            ):
                raise ApiError(
                    409,
                    "import_task_conflict",
                    "同一媒体库已有导入任务",
                    {"library_id": library_id},
                )
        for library_tasks in tasks_by_library.values():
            ImportTaskService.enqueue_batch(
                library_tasks,
                trigger_type="manual",
                allowed_statuses=retryable_statuses,
            )
        return DownloadTaskBatchImportResponse(
            accepted_count=len(retryable_tasks),
            skipped_task_ids=skipped_task_ids,
        )

    @classmethod
    def list_task_files(cls, task_id: int) -> list[DownloadTaskFileResource]:
        """列出下载任务源内的文件；仅已完成且带来源引用的任务可查看。"""
        task = require_task(task_id)
        if task.completed_source_ref is None:
            raise ApiError(
                422,
                "download_task_files_unavailable",
                "该下载任务没有可查看的文件",
                {"task_id": task.id},
            )
        library = task.client.library
        try:
            storage = MEDIA_PROVIDER_REGISTRY.storage_for(library_handle_for(library))
        except ProviderUnavailableError as exc:
            raise ApiError(
                503,
                "provider_not_installed",
                "媒体提供方未安装",
                {"provider_key": library.provider_key},
            ) from exc
        try:
            scanned = tuple(
                storage.scan_import_source(source_ref=task.completed_source_ref)
            )
        except ProviderOperationError as exc:
            raise cls._provider_error(exc) from exc
        except Exception as exc:
            raise ApiError(
                502,
                "download_task_files_failed",
                "下载任务文件读取失败",
                {"task_id": task.id},
            ) from exc
        files = sorted(
            scanned,
            key=lambda item: (item.relative_path.casefold(), item.relative_path),
        )
        return [
            DownloadTaskFileResource(
                name=file.name,
                relative_path=file.relative_path,
                size_bytes=file.size_bytes,
                is_video=file.is_video,
            )
            for file in files
        ]

    @staticmethod
    def _provider_error(exc: ProviderOperationError) -> ApiError:
        status = {
            "invalid_config": 422,
            "authentication_failed": 401,
            "source_not_found": 404,
            "task_not_managed": 409,
            "unsupported": 422,
            "unavailable": 503,
        }.get(exc.code, 502)
        return ApiError(
            status,
            f"provider_{exc.code}",
            exc.safe_message,
            {"provider_key": exc.provider_key, "operation": exc.operation},
        )
