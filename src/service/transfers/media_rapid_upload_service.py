from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from peewee import IntegrityError

from src.api.exception.errors import ApiError
from src.common.database import ensure_database_ready
from src.common.runtime_time import utc_now_for_db
from src.lib.cloud115 import Cloud115NotFoundError, RapidUploadStatus
from src.model import (
    Media,
    MediaLibrary,
    MediaRapidUploadBatch,
    MediaRapidUploadItem,
    get_database,
)
from src.model.enums import MediaLibraryBackend
from src.schema.common.pagination import PageResponse
from src.schema.transfers.rapid_upload import (
    MediaRapidUploadBatchListItemResource,
    MediaRapidUploadBatchResource,
    MediaRapidUploadTriggerResponse,
)
from src.service.playback.cloud115_backend_service import (
    cloud115_client_for,
    require_cloud115_library,
)
from src.service.system import ActivityService
from src.service.transfers.cloud115_import_common import (
    cloud115_import_mutex_key,
    ensure_cloud115_jav_target_dir,
    ensure_cloud115_videos_target_dir,
    normalize_jav_media_filename,
    verify_cloud115_renamed_file,
)
from src.service.transfers.import_runner import MediaRapidUploadRunner


BATCH_STATE_PENDING = "pending"
BATCH_STATE_RUNNING = "running"
BATCH_STATE_COMPLETED = "completed"
BATCH_STATE_FAILED = "failed"

ITEM_STATE_PENDING = "pending"
ITEM_STATE_RUNNING = "running"
ITEM_STATE_REMOTE_UPLOADED = "remote_uploaded"
ITEM_STATE_SUCCEEDED = "succeeded"
ITEM_STATE_FAILED = "failed"
ITEM_STATE_CLEANUP_FAILED = "cleanup_failed"

ITEM_ACTION_RAPID_UPLOAD = "rapid_upload"
ITEM_ACTION_CLEANUP_ONLY = "cleanup_only"
ITEM_ACTION_RESUME_REMOTE = "resume_remote"
RETRYABLE_ITEM_STATES = {ITEM_STATE_FAILED, ITEM_STATE_CLEANUP_FAILED}

# 只在 state='failed' 时有值，用来把"115 库里没有相同 sha1"（重试无意义）跟其它
# 可重试失败区分开；对外 API 层用这个字段派生列表中的 last_rapid_upload_status。
FAILURE_REASON_NOT_HIT = "not_hit"
FAILURE_REASON_FILE_CHANGED = "file_changed"
FAILURE_REASON_REMOTE_ERROR = "remote_error"
FAILURE_REASON_VERIFICATION_FAILED = "verification_failed"
FAILURE_REASON_OTHER = "other"

TASK_KEY = "media_rapid_upload"
GENERIC_ITEM_ERROR = "秒传失败"
GENERIC_CLEANUP_ERROR = "本地文件删除失败"

# 对外暴露给 media 查询接口的紧凑值域；见 MediaRapidUploadService.get_latest_status_by_media。
PUBLIC_STATUS_NOT_HIT = "not_hit"
PUBLIC_STATUS_FAILED = "failed"
PUBLIC_STATUS_CLEANUP_FAILED = "cleanup_failed"
PUBLIC_STATUS_IN_PROGRESS = "in_progress"
_IN_PROGRESS_ITEM_STATES = frozenset(
    (ITEM_STATE_PENDING, ITEM_STATE_RUNNING, ITEM_STATE_REMOTE_UPLOADED)
)


class _RapidUploadFailure(RuntimeError):
    """带 failure_reason 的秒传失败信号，让 _process_batch 兜底能落到 failure_reason 列。

    非本类型的其它异常仍按 REMOTE_ERROR 归类，保持"未指明原因即视为可重试"。
    """

    def __init__(self, message: str, *, failure_reason: str) -> None:
        super().__init__(message)
        self.failure_reason = failure_reason


@dataclass(frozen=True)
class _ItemSpec:
    media_id: int
    action: str
    source_library_id: int | None
    source_path: str
    source_size_bytes: int
    source_mtime_ns: int
    source_sha1: str | None = None
    target_cid: str | None = None
    target_fid: str | None = None
    target_pickcode: str | None = None
    target_name: str | None = None
    retry_source_item_id: int | None = None


class MediaRapidUploadService:
    """把一批本地 Media 顺序秒传到同一个 115 媒体库，并原地切换存储定位。"""

    @classmethod
    def trigger_batch(
        cls,
        *,
        media_ids: list[int],
        target_library_id: int,
    ) -> MediaRapidUploadTriggerResponse:
        if not media_ids or len(media_ids) > 200 or len(media_ids) != len(set(media_ids)):
            raise ApiError(
                422,
                "invalid_media_rapid_upload_selection",
                "media_ids 必须包含 1 至 200 个不重复的媒体 ID",
            )
        target_library = cls._require_target_library(target_library_id)
        specs = [cls._build_local_item_spec(media_id) for media_id in media_ids]
        return cls._create_and_submit_batch(
            target_library=target_library,
            specs=specs,
            retry_of_batch_id=None,
        )

    @classmethod
    def retry_batch(cls, batch_id: int) -> MediaRapidUploadTriggerResponse:
        original = cls._require_batch(batch_id)
        if original.state not in {BATCH_STATE_COMPLETED, BATCH_STATE_FAILED}:
            raise ApiError(
                409,
                "media_rapid_upload_batch_in_progress",
                "秒传批次仍在执行中",
                {"rapid_upload_batch_id": batch_id},
            )
        target_library = cls._require_target_library(original.target_library_id)
        specs: list[_ItemSpec] = []
        items = (
            MediaRapidUploadItem.select()
            .where(
                MediaRapidUploadItem.batch == original,
                MediaRapidUploadItem.state.in_(RETRYABLE_ITEM_STATES),
            )
            .order_by(MediaRapidUploadItem.id.asc())
        )
        for item in items:
            if item.media_id is None:
                continue
            if item.state == ITEM_STATE_CLEANUP_FAILED:
                specs.append(cls._build_cleanup_retry_spec(item))
            elif cls._has_remote_locator(item):
                specs.append(cls._build_remote_resume_spec(item))
            else:
                specs.append(
                    cls._build_local_item_spec(
                        item.media_id,
                        retry_source_item_id=item.id,
                    )
                )
        if not specs:
            raise ApiError(
                422,
                "media_rapid_upload_no_retryable_items",
                "该批次没有可重试的失败媒体",
                {"rapid_upload_batch_id": batch_id},
            )
        return cls._create_and_submit_batch(
            target_library=target_library,
            specs=specs,
            retry_of_batch_id=original.id,
        )

    @classmethod
    def list_batches(cls, *, page: int = 1, page_size: int = 20) -> PageResponse:
        if page < 1 or page_size < 1 or page_size > 100:
            raise ApiError(422, "invalid_pagination", "分页参数非法")
        query = MediaRapidUploadBatch.select().order_by(
            MediaRapidUploadBatch.id.desc()
        )
        total = query.count()
        rows = list(query.offset((page - 1) * page_size).limit(page_size))
        return PageResponse[MediaRapidUploadBatchListItemResource](
            items=[MediaRapidUploadBatchListItemResource.from_model(row) for row in rows],
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_batch(cls, batch_id: int) -> MediaRapidUploadBatchResource:
        batch = cls._require_batch(batch_id)
        items = list(
            MediaRapidUploadItem.select()
            .where(MediaRapidUploadItem.batch == batch)
            .order_by(MediaRapidUploadItem.id.asc())
        )
        return MediaRapidUploadBatchResource.from_model(batch, items=items)

    @classmethod
    def get_latest_status_by_media(cls, media_ids: list[int]) -> dict[int, str]:
        """按 media_id 取最新一次非 retried 的 item，映射成对外紧凑 status。

        返回值域：``'not_hit' | 'failed' | 'cleanup_failed' | 'in_progress'``；
        缺席 key 即"该 media 未参与过秒传"或"最新一次已 succeeded"（succeeded 意味着 media
        已切云端，本地视角不需要该提示）。
        """
        if not media_ids:
            return {}
        # PostgreSQL DISTINCT ON：按 media_id 分组、id DESC 取每组第一条，一趟走
        # (media_id) 索引即可拿到"最新一次非 retried item"；比 IN(SELECT MAX GROUP BY)
        # 子查询少一次扫描。state != 'retried' 排除重试接管后的老条目。
        rows = (
            MediaRapidUploadItem.select(
                MediaRapidUploadItem.media,
                MediaRapidUploadItem.state,
                MediaRapidUploadItem.failure_reason,
            )
            .where(
                MediaRapidUploadItem.media.in_(media_ids),
                MediaRapidUploadItem.state != "retried",
            )
            .order_by(MediaRapidUploadItem.media, MediaRapidUploadItem.id.desc())
            .distinct(MediaRapidUploadItem.media)
        )
        result: dict[int, str] = {}
        for row in rows:
            status = cls._map_item_to_public_status(row.state, row.failure_reason)
            if status is not None:
                result[row.media_id] = status
        return result

    @staticmethod
    def _map_item_to_public_status(state: str, failure_reason: str | None) -> str | None:
        # 内部 state/reason 到对外紧凑值域的唯一映射点，新增值域时改这一处。
        if state == ITEM_STATE_SUCCEEDED:
            return None
        if state in _IN_PROGRESS_ITEM_STATES:
            return PUBLIC_STATUS_IN_PROGRESS
        if state == ITEM_STATE_CLEANUP_FAILED:
            return PUBLIC_STATUS_CLEANUP_FAILED
        if state == ITEM_STATE_FAILED:
            if failure_reason == FAILURE_REASON_NOT_HIT:
                return PUBLIC_STATUS_NOT_HIT
            return PUBLIC_STATUS_FAILED
        return None

    @staticmethod
    def has_active_media(media_id: int) -> bool:
        return (
            MediaRapidUploadItem.select()
            .where(
                (MediaRapidUploadItem.active_media_id == media_id)
                | (
                    (MediaRapidUploadItem.media == media_id)
                    & (MediaRapidUploadItem.state == ITEM_STATE_CLEANUP_FAILED)
                )
            )
            .exists()
        )

    @staticmethod
    def has_active_library(library_id: int) -> bool:
        return (
            MediaRapidUploadBatch.select()
            .where(
                MediaRapidUploadBatch.target_library == library_id,
                MediaRapidUploadBatch.state.in_((BATCH_STATE_PENDING, BATCH_STATE_RUNNING)),
            )
            .exists()
        )

    @staticmethod
    def has_library_history(library_id: int) -> bool:
        """秒传历史保留目标媒体库外键，删除前必须显式拦截。"""
        return (
            MediaRapidUploadBatch.select()
            .where(MediaRapidUploadBatch.target_library == library_id)
            .exists()
        )

    @classmethod
    def recover_interrupted_batches(cls) -> dict[str, int]:
        """启动时把中断批次收敛为可重试终态；已切云端的条目仅重试清源。"""
        recovered = 0
        batches = MediaRapidUploadBatch.select().where(
            MediaRapidUploadBatch.state.in_((BATCH_STATE_PENDING, BATCH_STATE_RUNNING))
        )
        for batch in batches:
            for item in MediaRapidUploadItem.select().where(
                MediaRapidUploadItem.batch == batch,
                MediaRapidUploadItem.state.not_in(
                    (ITEM_STATE_SUCCEEDED, ITEM_STATE_FAILED, ITEM_STATE_CLEANUP_FAILED)
                ),
            ):
                media = Media.get_or_none(Media.id == item.media_id)
                if (
                    media is not None
                    and media.library_id == batch.target_library_id
                    and media.path is None
                    and item.target_pickcode
                ):
                    item.state = ITEM_STATE_CLEANUP_FAILED
                    item.error_message = GENERIC_CLEANUP_ERROR
                else:
                    item.state = ITEM_STATE_FAILED
                    item.error_message = GENERIC_ITEM_ERROR
                    # 启动兜底不知道原始失败原因，标 OTHER 让前端按可重试处理。
                    item.failure_reason = FAILURE_REASON_OTHER
                item.active_media_id = None
                item.finished_at = utc_now_for_db()
                item.updated_at = utc_now_for_db()
                item.save()
            cls._finish_batch(batch, force_failed=True)
            recovered += 1

        # 已结束但因短暂故障漏发通知的批次，也在启动恢复时补齐。
        for batch in MediaRapidUploadBatch.select().where(
            MediaRapidUploadBatch.state.in_((BATCH_STATE_COMPLETED, BATCH_STATE_FAILED)),
            MediaRapidUploadBatch.completion_notification.is_null(True),
        ):
            if batch.task_run_id is None:
                continue
            try:
                cls._ensure_completion_notification(batch.id, batch.task_run_id)
            except Exception:
                # 通知投递不应阻断其他中断批次的状态收敛；下次启动会继续补发。
                logger.exception(
                    "Media rapid upload recovery notification failed batch_id={}",
                    batch.id,
                )
        return {"recovered_count": recovered}

    @classmethod
    def _create_and_submit_batch(
        cls,
        *,
        target_library: MediaLibrary,
        specs: list[_ItemSpec],
        retry_of_batch_id: int | None,
    ) -> MediaRapidUploadTriggerResponse:
        try:
            task_run = ActivityService.create_task_run(
                task_key=TASK_KEY,
                task_name=f"批量媒体秒传（{len(specs)} 个）",
                trigger_type="manual",
                mutex_key=cloud115_import_mutex_key(target_library.id),
            )
        except IntegrityError as exc:
            blocking = ActivityService.find_task_run_by_mutex_key(
                cloud115_import_mutex_key(target_library.id)
            )
            raise ApiError(
                409,
                "media_rapid_upload_conflict",
                "该 115 媒体库已有写入任务正在执行",
                {"blocking_task_run_id": blocking.id if blocking is not None else None},
            ) from exc

        batch: MediaRapidUploadBatch | None = None
        try:
            with get_database().atomic():
                batch = MediaRapidUploadBatch.create(
                    target_library=target_library,
                    retry_of_batch=retry_of_batch_id,
                    task_run=task_run,
                    state=BATCH_STATE_PENDING,
                    total_count=len(specs),
                )
                for spec in specs:
                    MediaRapidUploadItem.create(
                        batch=batch,
                        media=spec.media_id,
                        active_media_id=spec.media_id,
                        action=spec.action,
                        state=ITEM_STATE_PENDING,
                        source_library_id=spec.source_library_id,
                        source_path=spec.source_path,
                        source_size_bytes=spec.source_size_bytes,
                        source_mtime_ns=spec.source_mtime_ns,
                        source_sha1=spec.source_sha1,
                        target_cid=spec.target_cid,
                        target_fid=spec.target_fid,
                        target_pickcode=spec.target_pickcode,
                        target_name=spec.target_name,
                    )
                retry_source_item_ids = [
                    spec.retry_source_item_id
                    for spec in specs
                    if spec.retry_source_item_id is not None
                ]
                if retry_source_item_ids:
                    # 新批次接管失败项后，旧项不再重复参与后续重试或删除保护。
                    (
                        MediaRapidUploadItem.update(
                            state="retried",
                            updated_at=utc_now_for_db(),
                        )
                        .where(MediaRapidUploadItem.id.in_(retry_source_item_ids))
                        .execute()
                    )
            MediaRapidUploadRunner.submit(batch.id, cls._run_batch, batch.id, task_run.id)
        except IntegrityError as exc:
            ActivityService.fail_task_run(
                task_run.id,
                error_message="媒体已在其它秒传批次中",
            )
            raise ApiError(
                409,
                "media_rapid_upload_media_conflict",
                "选择的媒体已在其它秒传批次中",
            ) from exc
        except Exception as exc:
            if batch is not None:
                cls._fail_unstarted_batch(batch, str(exc))
            ActivityService.fail_task_run(task_run.id, error_message=str(exc))
            raise ApiError(
                502,
                "media_rapid_upload_launch_failed",
                "秒传任务入队失败",
                {"detail": str(exc)},
            ) from exc
        return MediaRapidUploadTriggerResponse(
            rapid_upload_batch_id=batch.id,
            task_run_id=task_run.id,
            status="accepted",
        )

    @classmethod
    def _run_batch(cls, batch_id: int, task_run_id: int) -> dict:
        ensure_database_ready()

        def _task(reporter):
            batch = MediaRapidUploadBatch.get_by_id(batch_id)
            batch.state = BATCH_STATE_RUNNING
            batch.started_at = utc_now_for_db()
            batch.updated_at = utc_now_for_db()
            batch.save()
            asyncio.run(cls._process_batch(batch, reporter))
            cls._finish_batch(batch)
            batch = MediaRapidUploadBatch.get_by_id(batch_id)
            return {
                "rapid_upload_batch_id": batch.id,
                "total_count": batch.total_count,
                "succeeded_count": batch.succeeded_count,
                "failed_count": batch.failed_count,
                "cleanup_failed_count": batch.cleanup_failed_count,
            }

        try:
            result = ActivityService.run_task(
                task_key=TASK_KEY,
                trigger_type="internal",
                task_run_id=task_run_id,
                func=_task,
                notify_result=False,
            )
        except Exception as exc:
            logger.exception("Media rapid upload batch crashed batch_id={}", batch_id)
            batch = MediaRapidUploadBatch.get_by_id(batch_id)
            cls._fail_unfinished_items(batch, detail=str(exc))
            cls._finish_batch(batch, force_failed=True)
            result = {
                "rapid_upload_batch_id": batch_id,
                "total_count": batch.total_count,
                "succeeded_count": batch.succeeded_count,
                "failed_count": batch.failed_count,
                "cleanup_failed_count": batch.cleanup_failed_count,
            }
        cls._ensure_completion_notification(batch_id, task_run_id)
        return result

    @classmethod
    async def _process_batch(cls, batch, reporter) -> None:
        target_library = MediaLibrary.get_by_id(batch.target_library_id)
        config = require_cloud115_library(target_library)
        items = list(
            MediaRapidUploadItem.select()
            .where(MediaRapidUploadItem.batch == batch)
            .order_by(MediaRapidUploadItem.id.asc())
        )
        async with cloud115_client_for(target_library) as client:
            for index, item in enumerate(items, start=1):
                item.state = ITEM_STATE_RUNNING
                item.started_at = utc_now_for_db()
                item.error_message = None
                item.updated_at = utc_now_for_db()
                item.save()
                try:
                    if item.action == ITEM_ACTION_CLEANUP_ONLY:
                        try:
                            cls._cleanup_source(item)
                        except OSError as exc:
                            cls._mark_item_cleanup_failed(
                                item,
                                cls._format_error(exc, fallback=GENERIC_CLEANUP_ERROR),
                            )
                    elif item.action == ITEM_ACTION_RESUME_REMOTE:
                        media = cls._require_current_local_media(item)
                        await cls._resume_remote_item(
                            client,
                            item=item,
                            media=media,
                            target_library=target_library,
                        )
                    else:
                        media = cls._require_current_local_media(item)
                        # 每个 media 独立建版本目录，跟本地 create_version_directory 对齐；
                        # 同批次里两条 media 不共享版本目录。
                        target_cid, target_name = await cls._prepare_target_dir_and_name(
                            client,
                            root_cid=config["root_cid"],
                            media=media,
                            source_path=item.source_path,
                        )
                        await cls._rapid_upload_item(
                            client,
                            item=item,
                            media=media,
                            target_library=target_library,
                            target_cid=target_cid,
                            target_name=target_name,
                        )
                except Exception as exc:
                    logger.warning(
                        "Media rapid upload item failed batch_id={} item_id={} media_id={} detail={}",
                        batch.id,
                        item.id,
                        item.media_id,
                        exc,
                    )
                    # _RapidUploadFailure 明确告知失败原因；未指明的按 REMOTE_ERROR 处理，
                    # 语义上"可重试"，跟 NOT_HIT 区分开。
                    failure_reason = (
                        exc.failure_reason
                        if isinstance(exc, _RapidUploadFailure)
                        else FAILURE_REASON_REMOTE_ERROR
                    )
                    cls._mark_item_failed(
                        item,
                        cls._format_error(exc),
                        failure_reason=failure_reason,
                    )
                counts = cls._item_counts(batch.id)
                reporter.emit(
                    current=index,
                    total=len(items),
                    text=f"已处理 {index}/{len(items)} 个媒体",
                    summary_patch=counts,
                )

    @staticmethod
    async def _prepare_target_dir_and_name(
        client,
        *,
        root_cid: str,
        media: Media,
        source_path: str,
    ) -> tuple[str, str]:
        """按 media 类型建 <root>/jav/{番号}/{版本ms}/ 或 <root>/videos/{video_id}/{版本ms}/。

        返回 (版本目录 cid, 目标文件名)。JAV 命名规范化为 ``{番号}{ext}``；videos 保留
        原文件名，跟本地 MediaImportWriter / VideoImportService 对齐。
        """
        now_ms = int(time.time() * 1000)
        source_name = Path(source_path).name
        if media.movie_number:
            _entity_cid, version_cid = await ensure_cloud115_jav_target_dir(
                client,
                root_cid=root_cid,
                movie_number=media.movie_number,
                now_ms=now_ms,
            )
            target_name = normalize_jav_media_filename(media.movie_number, source_name)
        else:
            if media.video_item_id is None:
                raise RuntimeError("media is neither JAV nor videos, cannot pick target dir")
            _entity_cid, version_cid = await ensure_cloud115_videos_target_dir(
                client,
                root_cid=root_cid,
                video_id=media.video_item_id,
                now_ms=now_ms,
            )
            target_name = source_name
        return version_cid, target_name

    @classmethod
    async def _rapid_upload_item(
        cls,
        client,
        *,
        item: MediaRapidUploadItem,
        media: Media,
        target_library: MediaLibrary,
        target_cid: str,
        target_name: str,
    ) -> None:
        # target_cid 是 _prepare_target_dir_and_name 本次为该 media 新建的独占版本目录；
        # 任何抛出路径都要保证不给 115 侧留下空目录。
        file_created = False
        media_switched = False
        result = None
        try:
            result = await client.rapid_upload(item.source_path, pid=target_cid)
            if result.status is not RapidUploadStatus.SUCCESS:
                # NOT_HIT / FILE_CHANGED 都没有产生云端文件（file_created 仍为 False），
                # 走已有 except 分支只做空版本目录清理即可，其它回滚不必要。
                reason = (
                    FAILURE_REASON_NOT_HIT
                    if result.status is RapidUploadStatus.NOT_HIT
                    else FAILURE_REASON_FILE_CHANGED
                )
                raise _RapidUploadFailure(
                    f"rapid upload status={result.status.value}",
                    failure_reason=reason,
                )
            if not result.file_id or not result.pickcode:
                raise _RapidUploadFailure(
                    "rapid upload success response missing fid or pickcode",
                    failure_reason=FAILURE_REASON_REMOTE_ERROR,
                )
            file_created = True
            # 秒传接口一旦成功，立刻持久化远端定位。后续校验或回滚失败时，
            # 重试可以接管这个远端文件，不能再次秒传产生重复文件。
            item.source_sha1 = result.sha1.upper()
            item.target_cid = target_cid
            item.target_fid = result.file_id
            item.target_pickcode = result.pickcode
            item.target_name = target_name
            item.state = ITEM_STATE_REMOTE_UPLOADED
            item.updated_at = utc_now_for_db()
            item.save()
            meta = await client.file_info(result.file_id)
            if (
                meta.parent_id != target_cid
                or meta.size != result.size
                or meta.sha1.upper() != result.sha1.upper()
                or meta.pickcode != result.pickcode
            ):
                raise _RapidUploadFailure(
                    "rapid upload verification failed",
                    failure_reason=FAILURE_REASON_VERIFICATION_FAILED,
                )

            if meta.name != target_name:
                await client.rename_file(result.file_id, target_name)
                await verify_cloud115_renamed_file(client, result.file_id, target_name)

            # SDK 校验覆盖哈希阶段；入库前再核对一次，避免上传请求期间源路径被替换。
            if not cls._snapshot_matches(item, Path(item.source_path).stat()):
                raise _RapidUploadFailure(
                    "source file changed during rapid upload",
                    failure_reason=FAILURE_REASON_FILE_CHANGED,
                )

            cls._switch_media_to_remote(
                media=media,
                item=item,
                target_library=target_library,
                file_size_bytes=result.size,
            )
            media_switched = True
        except Exception:
            # 已切云端（权威定位在 Media 上）就什么都不动；否则按已推进阶段反向回收。
            if not media_switched:
                file_rolled_back = not file_created
                if file_created and result is not None:
                    try:
                        await client.delete_files([result.file_id], pid=target_cid)
                        item.source_sha1 = None
                        item.target_cid = None
                        item.target_fid = None
                        item.target_pickcode = None
                        item.target_name = None
                        item.updated_at = utc_now_for_db()
                        item.save()
                        file_rolled_back = True
                    except Exception as cleanup_exc:
                        # 回收文件失败必须保留 item 定位，供重试接管已有远端文件；
                        # 此时也不再动版本目录，避免与残留文件解耦丢失定位。
                        logger.warning(
                            "Rapid upload remote rollback failed item_id={} fid={} detail={}",
                            item.id,
                            result.file_id,
                            cleanup_exc,
                        )
                # 只有确认版本目录是空的（未产生文件或文件已回收）才动它，删了
                # 避免反复失败在 jav/{番号}/ 或 videos/{video_id}/ 下累积空目录。
                if file_rolled_back:
                    try:
                        await client.delete_files([target_cid])
                    except Exception as cleanup_exc:
                        logger.warning(
                            "Rapid upload remote version dir cleanup failed item_id={} version_cid={} detail={}",
                            item.id,
                            target_cid,
                            cleanup_exc,
                        )
            raise
        assert result is not None  # media_switched 为真必然经过 rapid_upload 成功

        try:
            cls._cleanup_source(item)
        except OSError as exc:
            logger.warning(
                "Media rapid upload local cleanup failed item_id={} path={} detail={}",
                item.id,
                item.source_path,
                exc,
            )
            cls._mark_item_cleanup_failed(
                item,
                cls._format_error(exc, fallback=GENERIC_CLEANUP_ERROR),
            )

    @classmethod
    async def _resume_remote_item(
        cls,
        client,
        *,
        item: MediaRapidUploadItem,
        media: Media,
        target_library: MediaLibrary,
    ) -> None:
        """恢复已确认落到 115、但尚未完成 Media 切换的中断条目。"""
        try:
            meta = await client.file_info(item.target_fid)
        except Cloud115NotFoundError:
            # 远端已不存在，清掉恢复定位；下次重试会重新执行秒传。
            item.source_sha1 = None
            item.target_cid = None
            item.target_fid = None
            item.target_pickcode = None
            item.target_name = None
            item.updated_at = utc_now_for_db()
            item.save()
            raise
        if (
            meta.parent_id != item.target_cid
            or meta.size != item.source_size_bytes
            or meta.sha1.upper() != item.source_sha1.upper()
            or meta.pickcode != item.target_pickcode
        ):
            raise _RapidUploadFailure(
                "interrupted rapid upload verification failed",
                failure_reason=FAILURE_REASON_VERIFICATION_FAILED,
            )
        # 中断项在首次落远端时就写入了 target_name，正常路径直接沿用；老结构中断
        # 保留旧命名不改动。target_name 缺失属于病态数据，按当前 media 类型重建。
        target_name = item.target_name
        if not target_name:
            source_name = Path(item.source_path).name
            target_name = (
                normalize_jav_media_filename(media.movie_number, source_name)
                if media.movie_number
                else source_name
            )
        if meta.name != target_name:
            await client.rename_file(item.target_fid, target_name)
            await verify_cloud115_renamed_file(client, item.target_fid, target_name)
        item.target_name = target_name
        item.state = ITEM_STATE_REMOTE_UPLOADED
        item.updated_at = utc_now_for_db()
        item.save()
        if not cls._snapshot_matches(item, Path(item.source_path).stat()):
            raise _RapidUploadFailure(
                "source file changed before interrupted upload recovery",
                failure_reason=FAILURE_REASON_FILE_CHANGED,
            )
        cls._switch_media_to_remote(
            media=media,
            item=item,
            target_library=target_library,
            file_size_bytes=item.source_size_bytes,
        )
        try:
            cls._cleanup_source(item)
        except OSError as exc:
            cls._mark_item_cleanup_failed(
                item,
                cls._format_error(exc, fallback=GENERIC_CLEANUP_ERROR),
            )

    @staticmethod
    def _switch_media_to_remote(
        *,
        media: Media,
        item: MediaRapidUploadItem,
        target_library: MediaLibrary,
        file_size_bytes: int,
    ) -> None:
        locator = {
            "fid": item.target_fid,
            "pickcode": item.target_pickcode,
            "name": item.target_name,
            "source_path": item.source_path,
        }
        with get_database().atomic():
            current_media = Media.get_by_id(media.id)
            if current_media.path != item.source_path:
                raise _RapidUploadFailure(
                    "media storage changed during rapid upload",
                    failure_reason=FAILURE_REASON_FILE_CHANGED,
                )
            current_media.library = target_library
            current_media.path = None
            current_media.backend_locator = locator
            current_media.storage_mode = "rapid_upload"
            current_media.content_fingerprint = f"sha1:{item.source_sha1.upper()}"
            current_media.file_size_bytes = file_size_bytes
            current_media.updated_at = utc_now_for_db()
            current_media.save()

    @classmethod
    def _cleanup_source(cls, item: MediaRapidUploadItem) -> None:
        source = Path(item.source_path)
        try:
            stat = source.stat()
        except FileNotFoundError:
            cls._mark_item_succeeded(item)
            return
        if not cls._snapshot_matches(item, stat):
            raise OSError("source file changed before cleanup")
        source.unlink()
        cls._mark_item_succeeded(item)

    @staticmethod
    def _snapshot_matches(item: MediaRapidUploadItem, stat) -> bool:
        return (
            stat.st_size == item.source_size_bytes
            and stat.st_mtime_ns == item.source_mtime_ns
        )

    @classmethod
    def _require_current_local_media(cls, item: MediaRapidUploadItem) -> Media:
        media = Media.get_or_none(Media.id == item.media_id)
        if media is None or not media.path or media.path != item.source_path:
            raise _RapidUploadFailure(
                "local media is unavailable",
                failure_reason=FAILURE_REASON_OTHER,
            )
        if media.library_id is None or media.library.backend != MediaLibraryBackend.LOCAL.value:
            raise _RapidUploadFailure(
                "media is not stored in a local library",
                failure_reason=FAILURE_REASON_OTHER,
            )
        stat = Path(media.path).stat()
        if not cls._snapshot_matches(item, stat):
            raise _RapidUploadFailure(
                "source file changed before rapid upload",
                failure_reason=FAILURE_REASON_FILE_CHANGED,
            )
        return media

    @classmethod
    def _build_local_item_spec(
        cls,
        media_id: int,
        *,
        retry_source_item_id: int | None = None,
    ) -> _ItemSpec:
        media = Media.get_or_none(Media.id == media_id)
        if media is None:
            raise ApiError(404, "media_not_found", "媒体不存在", {"media_id": media_id})
        if media.library_id is None or media.library.backend != MediaLibraryBackend.LOCAL.value:
            raise ApiError(
                422,
                "media_rapid_upload_source_not_local",
                "只能秒传本地媒体库中的媒体",
                {"media_id": media_id},
            )
        if not media.path:
            raise ApiError(422, "media_file_not_found", "媒体没有本地文件", {"media_id": media_id})
        source = Path(media.path)
        try:
            stat = source.stat()
        except OSError as exc:
            raise ApiError(
                422,
                "media_file_not_found",
                "媒体本地文件不存在",
                {"media_id": media_id},
            ) from exc
        if not source.is_file():
            raise ApiError(422, "media_file_not_found", "媒体本地文件不存在", {"media_id": media_id})
        return _ItemSpec(
            media_id=media.id,
            action=ITEM_ACTION_RAPID_UPLOAD,
            source_library_id=media.library_id,
            source_path=str(source),
            source_size_bytes=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
            retry_source_item_id=retry_source_item_id,
        )

    @staticmethod
    def _build_cleanup_retry_spec(item: MediaRapidUploadItem) -> _ItemSpec:
        media = Media.get_or_none(Media.id == item.media_id)
        if media is None:
            raise ApiError(
                422,
                "media_rapid_upload_cleanup_target_missing",
                "待清理媒体已不存在，无法重试",
                {"media_id": item.media_id},
            )
        return _ItemSpec(
            media_id=item.media_id,
            action=ITEM_ACTION_CLEANUP_ONLY,
            source_library_id=item.source_library_id,
            source_path=item.source_path,
            source_size_bytes=item.source_size_bytes,
            source_mtime_ns=item.source_mtime_ns,
            source_sha1=item.source_sha1,
            target_cid=item.target_cid,
            target_fid=item.target_fid,
            target_pickcode=item.target_pickcode,
            target_name=item.target_name,
            retry_source_item_id=item.id,
        )

    @staticmethod
    def _build_remote_resume_spec(item: MediaRapidUploadItem) -> _ItemSpec:
        return _ItemSpec(
            media_id=item.media_id,
            action=ITEM_ACTION_RESUME_REMOTE,
            source_library_id=item.source_library_id,
            source_path=item.source_path,
            source_size_bytes=item.source_size_bytes,
            source_mtime_ns=item.source_mtime_ns,
            source_sha1=item.source_sha1,
            target_cid=item.target_cid,
            target_fid=item.target_fid,
            target_pickcode=item.target_pickcode,
            target_name=item.target_name,
            retry_source_item_id=item.id,
        )

    @staticmethod
    def _has_remote_locator(item: MediaRapidUploadItem) -> bool:
        return all(
            (
                item.source_sha1,
                item.target_cid,
                item.target_fid,
                item.target_pickcode,
            )
        )

    @staticmethod
    def _require_target_library(library_id: int) -> MediaLibrary:
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ApiError(
                404,
                "media_library_not_found",
                "媒体库不存在",
                {"library_id": library_id},
            )
        if library.backend != MediaLibraryBackend.CLOUD115.value:
            raise ApiError(
                422,
                "media_rapid_upload_target_not_cloud115",
                "秒传目标必须是 115 媒体库",
                {"library_id": library_id},
            )
        require_cloud115_library(library)
        return library

    @staticmethod
    def _require_batch(batch_id: int) -> MediaRapidUploadBatch:
        batch = MediaRapidUploadBatch.get_or_none(MediaRapidUploadBatch.id == batch_id)
        if batch is None:
            raise ApiError(
                404,
                "media_rapid_upload_batch_not_found",
                "秒传批次不存在",
                {"rapid_upload_batch_id": batch_id},
            )
        return batch

    @staticmethod
    def _mark_item_succeeded(item: MediaRapidUploadItem) -> None:
        item.state = ITEM_STATE_SUCCEEDED
        item.active_media_id = None
        item.error_message = None
        item.finished_at = utc_now_for_db()
        item.updated_at = utc_now_for_db()
        item.save()

    @staticmethod
    def _mark_item_failed(
        item: MediaRapidUploadItem,
        message: str,
        *,
        failure_reason: str = FAILURE_REASON_OTHER,
    ) -> None:
        item.state = ITEM_STATE_FAILED
        item.active_media_id = None
        item.error_message = message
        item.failure_reason = failure_reason
        item.finished_at = utc_now_for_db()
        item.updated_at = utc_now_for_db()
        item.save()

    @staticmethod
    def _mark_item_cleanup_failed(
        item: MediaRapidUploadItem,
        message: str = GENERIC_CLEANUP_ERROR,
    ) -> None:
        item.state = ITEM_STATE_CLEANUP_FAILED
        item.active_media_id = None
        item.error_message = message
        item.finished_at = utc_now_for_db()
        item.updated_at = utc_now_for_db()
        item.save()

    # 前端只能看到 error_message，异常原文入库能省一次翻服务器日志的功夫。
    # 空串或空白回退到通用文案，避免 UI 展示空白无信息。
    _ERROR_MESSAGE_MAX_LEN = 500

    @classmethod
    def _format_error(cls, exc: BaseException, *, fallback: str = GENERIC_ITEM_ERROR) -> str:
        text = str(exc).strip()
        if not text:
            return fallback
        if len(text) <= cls._ERROR_MESSAGE_MAX_LEN:
            return text
        return text[: cls._ERROR_MESSAGE_MAX_LEN - 1] + "…"

    @staticmethod
    def _item_counts(batch_id: int) -> dict[str, int]:
        items = MediaRapidUploadItem.select().where(MediaRapidUploadItem.batch == batch_id)
        states = [item.state for item in items]
        return {
            "succeeded_count": states.count(ITEM_STATE_SUCCEEDED),
            "failed_count": states.count(ITEM_STATE_FAILED),
            "cleanup_failed_count": states.count(ITEM_STATE_CLEANUP_FAILED),
        }

    @classmethod
    def _finish_batch(cls, batch: MediaRapidUploadBatch, *, force_failed: bool = False) -> None:
        counts = cls._item_counts(batch.id)
        batch.succeeded_count = counts["succeeded_count"]
        batch.failed_count = counts["failed_count"]
        batch.cleanup_failed_count = counts["cleanup_failed_count"]
        batch.state = BATCH_STATE_FAILED if force_failed else BATCH_STATE_COMPLETED
        batch.finished_at = utc_now_for_db()
        batch.updated_at = utc_now_for_db()
        batch.save()

    @classmethod
    def _fail_unfinished_items(cls, batch, *, detail: str) -> None:
        # 兜底路径拿不到具体条目的异常，只有批次级 detail；空 detail 时回退到通用文案。
        message = (detail or "").strip() or GENERIC_ITEM_ERROR
        if len(message) > cls._ERROR_MESSAGE_MAX_LEN:
            message = message[: cls._ERROR_MESSAGE_MAX_LEN - 1] + "…"
        for item in MediaRapidUploadItem.select().where(
            MediaRapidUploadItem.batch == batch,
            MediaRapidUploadItem.active_media_id.is_null(False),
        ):
            logger.warning(
                "Media rapid upload unfinished item recovered item_id={} detail={}",
                item.id,
                detail,
            )
            # 批次级兜底看不到具体条目原因，归为 OTHER（前端按"可重试"展示）。
            cls._mark_item_failed(item, message, failure_reason=FAILURE_REASON_OTHER)

    @classmethod
    def _fail_unstarted_batch(cls, batch, detail: str) -> None:
        cls._fail_unfinished_items(batch, detail=detail)
        cls._finish_batch(batch, force_failed=True)

    @classmethod
    def _ensure_completion_notification(cls, batch_id: int, task_run_id: int) -> None:
        with get_database().atomic():
            batch = (
                MediaRapidUploadBatch.select()
                .where(MediaRapidUploadBatch.id == batch_id)
                .for_update()
                .get()
            )
            if batch.completion_notification_id is not None:
                return
            notification = ActivityService.create_media_rapid_upload_notification(
                batch_id=batch.id,
                task_run_id=task_run_id,
                total_count=batch.total_count,
                succeeded_count=batch.succeeded_count,
                failed_count=batch.failed_count,
                cleanup_failed_count=batch.cleanup_failed_count,
                batch_failed=batch.state == BATCH_STATE_FAILED,
            )
            batch.completion_notification = notification.id
            batch.updated_at = utc_now_for_db()
            batch.save(
                only=[
                    MediaRapidUploadBatch.completion_notification,
                    MediaRapidUploadBatch.updated_at,
                ]
            )
