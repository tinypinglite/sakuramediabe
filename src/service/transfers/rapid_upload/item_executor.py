from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.lib.cloud115 import Cloud115NotFoundError, RapidUploadStatus
from src.model import (
    Media,
    MediaLibrary,
    MediaRapidUploadItem,
    get_database,
)
from src.model.enums import MediaLibraryBackend
from src.service.transfers.cloud115.importer.common import (
    Cloud115TargetDirResolver,
    normalize_jav_media_filename,
    verify_cloud115_renamed_file,
)
from src.service.transfers.cloud115.importer.media_registrar import (
    Cloud115MediaRegistrar,
)
from src.service.transfers.rapid_upload.states import (
    FAILURE_REASON_FILE_CHANGED,
    FAILURE_REASON_NOT_HIT,
    FAILURE_REASON_OTHER,
    FAILURE_REASON_REMOTE_ERROR,
    FAILURE_REASON_VERIFICATION_FAILED,
    GENERIC_CLEANUP_ERROR,
)
from src.service.transfers.rapid_upload.types import RapidUploadFailure


class MediaRapidUploadItemExecutor:
    @staticmethod
    async def _prepare_target_dir_and_name(
        resolver: Cloud115TargetDirResolver,
        *,
        media: Media,
        source_path: str,
    ) -> tuple[str, str]:
        """按 media 类型建 <root>/jav/{番号}/{版本ms}/ 或 <root>/videos/{video_id}/{版本ms}/。

        返回 (版本目录 cid, 目标文件名)。JAV 命名规范化为 ``{番号}{ext}``；videos 保留
        原文件名，跟本地 MediaImportWriter / VideoImportService 对齐。目录查找走批次级
        缓存（resolver），整批只翻一次 jav//videos/，避免每条 item 列目录触发风控。
        """
        now_ms = int(time.time() * 1000)
        source_name = Path(source_path).name
        if media.movie_number:
            version_cid = await resolver.prepare_jav_version_dir(
                movie_number=media.movie_number, now_ms=now_ms
            )
            target_name = normalize_jav_media_filename(media.movie_number, source_name)
        else:
            if media.video_item_id is None:
                raise RuntimeError("media is neither JAV nor videos, cannot pick target dir")
            version_cid = await resolver.prepare_videos_version_dir(
                video_id=media.video_item_id, now_ms=now_ms
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
                raise RapidUploadFailure(
                    f"rapid upload status={result.status.value}",
                    failure_reason=reason,
                )
            if not result.file_id or not result.pickcode:
                raise RapidUploadFailure(
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
            cls._mark_item_remote_uploaded(item)
            meta = await client.file_info(result.file_id)
            if (
                meta.parent_id != target_cid
                or meta.size != result.size
                or meta.sha1.upper() != result.sha1.upper()
                or meta.pickcode != result.pickcode
            ):
                raise RapidUploadFailure(
                    "rapid upload verification failed",
                    failure_reason=FAILURE_REASON_VERIFICATION_FAILED,
                )

            if meta.name != target_name:
                await client.rename_file(result.file_id, target_name)
                await verify_cloud115_renamed_file(client, result.file_id, target_name)

            # SDK 校验覆盖哈希阶段；入库前再核对一次，避免上传请求期间源路径被替换。
            if not cls._snapshot_matches(item, Path(item.source_path).stat()):
                raise RapidUploadFailure(
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
            raise RapidUploadFailure(
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
        cls._mark_item_remote_uploaded(item)
        if not cls._snapshot_matches(item, Path(item.source_path).stat()):
            raise RapidUploadFailure(
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
        locator = Cloud115MediaRegistrar.build_locator(
            fid=item.target_fid,
            pickcode=item.target_pickcode,
            name=item.target_name,
            source_path=item.source_path,
        )
        with get_database().atomic():
            current_media = Media.get_by_id(media.id)
            if current_media.path != item.source_path:
                raise RapidUploadFailure(
                    "media storage changed during rapid upload",
                    failure_reason=FAILURE_REASON_FILE_CHANGED,
                )
            Cloud115MediaRegistrar.apply_cloud115_fields(
                current_media,
                library=target_library,
                locator=locator,
                fingerprint=Cloud115MediaRegistrar.build_fingerprint(item.source_sha1),
                file_size_bytes=file_size_bytes,
                storage_mode="rapid_upload",
            )
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
            raise RapidUploadFailure(
                "local media is unavailable",
                failure_reason=FAILURE_REASON_OTHER,
            )
        if media.library_id is None or media.library.backend != MediaLibraryBackend.LOCAL.value:
            raise RapidUploadFailure(
                "media is not stored in a local library",
                failure_reason=FAILURE_REASON_OTHER,
            )
        stat = Path(media.path).stat()
        if not cls._snapshot_matches(item, stat):
            raise RapidUploadFailure(
                "source file changed before rapid upload",
                failure_reason=FAILURE_REASON_FILE_CHANGED,
            )
        return media
