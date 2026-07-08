"""媒体导入 service。

顶层编排 ``import_from_source``：调 ``media_source_scanner`` 完成扫描分组、并发抓取远端元数据、
再委托 ``media_import_writer`` 完成落库。ImportJob 状态维护与 DownloadTask 状态回写在这里收口。
"""

from concurrent.futures import Future, ThreadPoolExecutor
import json
import time
from pathlib import Path
from threading import RLock, local
from typing import Any, Callable, Dict, List, Literal

from loguru import logger
from pydantic import BaseModel

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import DownloadTask, ImportJob, MediaLibrary, Movie, get_database
from src.service.catalog import CatalogImportService, ImageDownloadError
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService
from src.service.transfers.media_import_writer import import_single_scanned_file, import_vr_media_group
from src.service.transfers.media_source_scanner import (
    ImportTransferMode,
    find_media_library_containing_path,
    parse_movie_number,
    scan_source_files,
)
# 导入状态/失败原因的取值统一收口到 media_import_status 模块。
from src.common.media_import_status import (
    FAILURE_REASON_IMAGE_DOWNLOAD_FAILED,
    FAILURE_REASON_IMPORT_JOB_CRASHED,
    FAILURE_REASON_MEDIA_IMPORT_FAILED,
    FAILURE_REASON_METADATA_FETCH_FAILED,
    FAILURE_REASON_METADATA_UPSERT_FAILED,
    FAILURE_REASON_MULTI_PART_MERGE_FAILED,
    FAILURE_REASON_RETRY_SOURCES_MISSING,
    IMPORT_JOB_STATE_COMPLETED,
    IMPORT_JOB_STATE_FAILED,
    IMPORT_JOB_STATE_PENDING,
    IMPORT_JOB_STATE_RUNNING,
    IMPORT_STATUS_COMPLETED,
    IMPORT_STATUS_FAILED,
    IMPORT_STATUS_RUNNING,
    make_failure_item,
)

# 为向后兼容旧调用方（例如 videos 域曾借用 MediaImportService 的私有静态方法）保留 re-export。
from src.service.transfers.file_transfer import delete_source_files as _delete_source_files


ImportProgressCallback = Callable[[Dict[str, object]], None]


class MetadataImportResult(BaseModel):
    """并发抓取远端元数据后的单条结果。"""

    movie_number: str
    movie_id: int | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None


class MediaImportService:
    """把待导入目录中的视频文件转换为本地媒体库记录。"""

    def __init__(
        self,
        provider: Any | None = None,
        image_downloader: Callable[[str, Path], None] | None = None,
        now_ms: Callable[[], int] | None = None,
        catalog_import_service: CatalogImportService | None = None,
        media_metadata_probe_service: MediaMetadataProbeService | None = None,
    ):
        self.image_downloader = image_downloader
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._catalog_persist_lock = RLock()
        self._worker_local = local()
        self._provider_factory = None if provider is not None else self._create_provider
        self._catalog_import_service_factory = (
            None if catalog_import_service is not None else self._create_catalog_import_service
        )
        self.media_metadata_probe_service = media_metadata_probe_service or MediaMetadataProbeService()
        self.provider = provider or self._create_provider()
        self.catalog_import_service = catalog_import_service or self._create_catalog_import_service()
        logger.info(
            "MediaImportService initialized javdb_host={} image_root={}",
            settings.metadata.javdb_host,
            settings.media.import_image_root_path,
        )

    def _create_provider(self):
        from src.metadata.factory import build_javdb_provider

        return build_javdb_provider()

    def _create_catalog_import_service(self) -> CatalogImportService:
        return CatalogImportService(
            image_downloader=self.image_downloader,
            persist_lock=self._catalog_persist_lock,
        )

    def _metadata_max_workers(self, total_movies: int) -> int:
        configured_workers = max(1, settings.metadata.import_metadata_max_workers)
        return min(configured_workers, total_movies)

    def _get_worker_provider(self):
        if self._provider_factory is None:
            return self.provider
        provider = getattr(self._worker_local, "provider", None)
        if provider is None:
            provider = self._provider_factory()
            self._worker_local.provider = provider
        return provider

    def _get_worker_catalog_import_service(self):
        if self._catalog_import_service_factory is None:
            return self.catalog_import_service
        catalog_import_service = getattr(self._worker_local, "catalog_import_service", None)
        if catalog_import_service is None:
            catalog_import_service = self._catalog_import_service_factory()
            self._worker_local.catalog_import_service = catalog_import_service
        return catalog_import_service

    def _ensure_worker_database_ready(self) -> None:
        database = get_database()
        if database.is_closed():
            database.connect()

    @staticmethod
    def _emit_progress(progress_callback: ImportProgressCallback | None, **payload: object) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    def _import_movie_metadata(self, movie_number: str) -> MetadataImportResult:
        self._ensure_worker_database_ready()
        provider = self._get_worker_provider()
        catalog_import_service = self._get_worker_catalog_import_service()

        try:
            detail = provider.get_movie_by_number(movie_number)
        except Exception as exc:
            logger.warning("Import metadata fetch failed movie_number={} detail={}", movie_number, exc)
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason=FAILURE_REASON_METADATA_FETCH_FAILED,
                failure_detail=str(exc),
            )

        try:
            movie = catalog_import_service.upsert_movie_from_javdb_detail(
                detail,
                force_subscribed=True,
            )
        except ImageDownloadError as exc:
            logger.warning("Import image download failed movie_number={} detail={}", movie_number, exc)
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason=FAILURE_REASON_IMAGE_DOWNLOAD_FAILED,
                failure_detail=str(exc),
            )
        except Exception as exc:
            logger.exception("Import metadata upsert failed movie_number={} detail={}", movie_number, exc)
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason=FAILURE_REASON_METADATA_UPSERT_FAILED,
                failure_detail=str(exc),
            )

        logger.info(
            "Import metadata upsert success movie_number={} movie_id={}",
            movie_number,
            movie.id,
        )
        return MetadataImportResult(
            movie_number=movie_number,
            movie_id=movie.id,
        )

    def import_from_source(
        self,
        source_path: str,
        library_id: int,
        *,
        download_task_id: int | None = None,
        import_job_id: int | None = None,
        progress_callback: ImportProgressCallback | None = None,
        transfer_mode: ImportTransferMode = "auto",
        only_files: List[str] | None = None,
    ) -> ImportJob:
        """执行一次完整的媒体导入，并把中间状态写回 ImportJob。

        ``only_files`` 提供时仅导入这些绝对路径，用于失败文件的子集重导。
        """
        if transfer_mode not in ("auto", "cleanup-source"):
            logger.warning("Import rejected invalid transfer mode transfer_mode={}", transfer_mode)
            raise ValueError("invalid_transfer_mode")

        source_entry = Path(source_path).expanduser().resolve()
        if not source_entry.exists() or (not source_entry.is_dir() and not source_entry.is_file()):
            logger.warning("Import rejected invalid source path source_path={}", source_path)
            raise ValueError("source_path_not_found")

        # 子集重导时把目标文件归一化为绝对路径集合，扫描阶段据此过滤。
        only_file_set: set[str] | None = None
        if only_files is not None:
            only_file_set = {str(Path(item).expanduser().resolve()) for item in only_files}

        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            logger.warning("Import rejected because media library not found library_id={}", library_id)
            raise ValueError("media_library_not_found")

        if transfer_mode == "cleanup-source":
            matched_library = find_media_library_containing_path(source_entry)
            if matched_library is not None:
                logger.warning(
                    "Import rejected cleanup-source inside media library source_path={} matched_library_id={} matched_library_root={}",
                    str(source_entry),
                    matched_library.id,
                    matched_library.root_path,
                )
                raise ValueError("cleanup_source_inside_media_library")

        download_task = None
        if download_task_id is not None:
            download_task = DownloadTask.get_or_none(DownloadTask.id == download_task_id)
            if download_task is None:
                logger.warning(
                    "Import rejected because download task not found download_task_id={}",
                    download_task_id,
                )
                raise ValueError("download_task_not_found")

        logger.info(
            "Import start source_path={} library_id={} library_root={} download_task_id={}",
            str(source_entry),
            library_id,
            library.root_path,
            download_task_id,
        )
        # 支持创建新任务，也支持复用已有 ImportJob 做重试，后者需要把统计字段全部重置。
        if import_job_id is None:
            job = ImportJob.create(
                source_path=str(source_entry),
                library=library,
                download_task=download_task,
                state=IMPORT_JOB_STATE_PENDING,
                transfer_mode=transfer_mode,
            )
        else:
            job = ImportJob.get_by_id(import_job_id)
            job.source_path = str(source_entry)
            job.library = library
            job.download_task = download_task
            job.state = IMPORT_JOB_STATE_PENDING
            job.transfer_mode = transfer_mode
            job.imported_count = 0
            job.skipped_count = 0
            job.failed_count = 0
            job.failed_files = "[]"
            job.started_at = None
            job.finished_at = None
            job.save()
        logger.info("Import job created job_id={} state={}", job.id, job.state)
        failure_items: List[Dict[str, str]] = []
        imported_count = 0
        skipped_count = 0
        failed_count = 0
        new_playable_movies: Dict[int, Dict[str, object]] = {}

        job.state = IMPORT_JOB_STATE_RUNNING
        job.started_at = utc_now_for_db()
        job.save()
        if download_task is not None:
            download_task.import_status = IMPORT_STATUS_RUNNING
            download_task.save()
        logger.info("Import job running job_id={}", job.id)

        try:
            # 第一阶段只扫描和分组文件，不碰远端元数据和目标媒体库。
            # scan 命中已入库文件时，cleanup-source 模式下仍需要清理源目录，走 callback 复用共享删源工具。
            def _cleanup_duplicate_sources(source_paths: List[Path]) -> int:
                return _delete_source_files(source_paths, failure_items, transfer_mode=transfer_mode)

            grouped_files, grouped_skipped_count, grouped_failed_count = scan_source_files(
                source_entry,
                failure_items,
                transfer_mode=transfer_mode,
                only_file_set=only_file_set,
                on_duplicate_source_paths=_cleanup_duplicate_sources,
            )
            skipped_count += grouped_skipped_count
            failed_count += grouped_failed_count
            logger.info(
                "Import scan completed job_id={} grouped_numbers={} skipped={} failed={}",
                job.id,
                len(grouped_files),
                grouped_skipped_count,
                grouped_failed_count,
            )
            # 子集重导若选中的源文件全部缺失（如已被清理/移动），不应静默判 completed，记任务级失败。
            if (
                only_file_set is not None
                and not grouped_files
                and grouped_skipped_count == 0
                and grouped_failed_count == 0
            ):
                failed_count += 1
                failure_items.append(
                    make_failure_item(source_entry, FAILURE_REASON_RETRY_SOURCES_MISSING, "待重导的源文件均已不存在")
                )
            total_movie_numbers = len(grouped_files)
            completed_movie_numbers = 0
            self._emit_progress(
                progress_callback,
                event="scan_complete",
                total_movies=total_movie_numbers,
                current=0,
                total=total_movie_numbers,
                text="媒体文件扫描完成",
                summary_patch={
                    "imported_count": imported_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count,
                    "new_playable_movies": list(new_playable_movies.values()),
                },
            )

            metadata_futures: Dict[str, Future[MetadataImportResult]] = {}
            if grouped_files:
                max_workers = self._metadata_max_workers(total_movie_numbers)
                with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="import-metadata") as executor:
                    for movie_number in grouped_files:
                        metadata_futures[movie_number] = executor.submit(self._import_movie_metadata, movie_number)

                    for movie_number, group in grouped_files.items():
                        logger.info(
                            "Import processing movie_number={} files={} job_id={}",
                            movie_number,
                            len(group.files),
                            job.id,
                        )
                        self._emit_progress(
                            progress_callback,
                            event="movie_started",
                            stage="metadata",
                            movie_number=movie_number,
                            completed_movies=completed_movie_numbers,
                            total_movies=total_movie_numbers,
                            imported_count=imported_count,
                            skipped_count=skipped_count,
                            failed_count=failed_count,
                            current=completed_movie_numbers,
                            total=total_movie_numbers,
                            text=f"正在抓取影片元数据 {movie_number}",
                            summary_patch={
                                "imported_count": imported_count,
                                "skipped_count": skipped_count,
                                "failed_count": failed_count,
                                "new_playable_movies": list(new_playable_movies.values()),
                            },
                        )

                        metadata_result = metadata_futures[movie_number].result()
                        if metadata_result.failure_reason is not None:
                            for file_entry in group.files:
                                failed_count += 1
                                failure_items.append(
                                    make_failure_item(
                                        file_entry.path,
                                        metadata_result.failure_reason,
                                        metadata_result.failure_detail or "",
                                    )
                                )
                            completed_movie_numbers += 1
                            self._emit_progress(
                                progress_callback,
                                event="movie_finished",
                                stage="metadata",
                                movie_number=movie_number,
                                completed_movies=completed_movie_numbers,
                                total_movies=total_movie_numbers,
                                imported_count=imported_count,
                                skipped_count=skipped_count,
                                failed_count=failed_count,
                                current=completed_movie_numbers,
                                total=total_movie_numbers,
                                text=f"影片元数据处理失败 {movie_number}",
                                summary_patch={
                                    "imported_count": imported_count,
                                    "skipped_count": skipped_count,
                                    "failed_count": failed_count,
                                    "new_playable_movies": list(new_playable_movies.values()),
                                },
                            )
                            continue

                        movie = Movie.get_by_id(metadata_result.movie_id)
                        self._emit_progress(
                            progress_callback,
                            event="movie_stage",
                            stage="import-media",
                            movie_number=movie_number,
                            completed_movies=completed_movie_numbers,
                            total_movies=total_movie_numbers,
                            imported_count=imported_count,
                            skipped_count=skipped_count,
                            failed_count=failed_count,
                            current=completed_movie_numbers,
                            total=total_movie_numbers,
                            text=f"正在导入影片文件 {movie_number}",
                            summary_patch={
                                "imported_count": imported_count,
                                "skipped_count": skipped_count,
                                "failed_count": failed_count,
                                "new_playable_movies": list(new_playable_movies.values()),
                            },
                        )

                        if group.merge_mode == "vr_concat":
                            try:
                                imported, delete_failed_count = import_vr_media_group(
                                    group=group,
                                    library=library,
                                    movie=movie,
                                    failure_items=failure_items,
                                    transfer_mode=transfer_mode,
                                    now_ms=self.now_ms,
                                    media_metadata_probe_service=self.media_metadata_probe_service,
                                )
                                if imported:
                                    imported_count += 1
                                    failed_count += delete_failed_count
                                    new_playable_movies[movie.id] = {
                                        "movie_id": movie.id,
                                        "movie_number": movie.movie_number,
                                        "title": movie.title,
                                    }
                                else:
                                    skipped_count += 1
                            except Exception as exc:
                                failed_count += 1
                                logger.exception(
                                    "Import VR media group failed job_id={} movie_number={} detail={}",
                                    job.id,
                                    movie_number,
                                    exc,
                                )
                                failure_items.append(
                                    make_failure_item(
                                        group.files[0].path,
                                        FAILURE_REASON_MULTI_PART_MERGE_FAILED,
                                        str(exc),
                                    )
                                )
                        else:
                            for file_entry in group.files:
                                try:
                                    imported, delete_failed_count = import_single_scanned_file(
                                        file_entry=file_entry,
                                        library=library,
                                        movie=movie,
                                        failure_items=failure_items,
                                        transfer_mode=transfer_mode,
                                        now_ms=self.now_ms,
                                        media_metadata_probe_service=self.media_metadata_probe_service,
                                    )
                                    if imported:
                                        imported_count += 1
                                        failed_count += delete_failed_count
                                        new_playable_movies[movie.id] = {
                                            "movie_id": movie.id,
                                            "movie_number": movie.movie_number,
                                            "title": movie.title,
                                        }
                                    else:
                                        skipped_count += 1
                                except Exception as exc:
                                    failed_count += 1
                                    logger.exception(
                                        "Import media failed job_id={} movie_number={} source={} detail={}",
                                        job.id,
                                        movie_number,
                                        str(file_entry.path),
                                        exc,
                                    )
                                    failure_items.append(
                                        make_failure_item(
                                            file_entry.path,
                                            FAILURE_REASON_MEDIA_IMPORT_FAILED,
                                            str(exc),
                                        )
                                    )

                        completed_movie_numbers += 1
                        self._emit_progress(
                            progress_callback,
                            event="movie_finished",
                            stage="import-media",
                            movie_number=movie_number,
                            completed_movies=completed_movie_numbers,
                            total_movies=total_movie_numbers,
                            imported_count=imported_count,
                            skipped_count=skipped_count,
                            failed_count=failed_count,
                            current=completed_movie_numbers,
                            total=total_movie_numbers,
                            text=f"影片导入完成 {movie_number}",
                            summary_patch={
                                "imported_count": imported_count,
                                "skipped_count": skipped_count,
                                "failed_count": failed_count,
                                "new_playable_movies": list(new_playable_movies.values()),
                            },
                        )

            # 整个导入过程中即使有单文件失败，也会把已成功结果保留下来，并以 failed 状态返回统计信息。
            job.imported_count = imported_count
            job.skipped_count = skipped_count
            job.failed_count = failed_count
            job.state = IMPORT_JOB_STATE_FAILED if failed_count > 0 else IMPORT_JOB_STATE_COMPLETED
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.finished_at = utc_now_for_db()
            job.save()
            if download_task is not None:
                download_task.import_status = IMPORT_STATUS_FAILED if failed_count > 0 else IMPORT_STATUS_COMPLETED
                download_task.save()
            logger.info(
                "Import job finished job_id={} state={} imported={} skipped={} failed={}",
                job.id,
                job.state,
                job.imported_count,
                job.skipped_count,
                job.failed_count,
            )
            self._emit_progress(
                progress_callback,
                event="job_finished",
                current=total_movie_numbers,
                total=total_movie_numbers,
                text="媒体导入任务完成",
                summary_patch={
                    "imported_count": imported_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count,
                    "new_playable_movies": list(new_playable_movies.values()),
                },
            )
            return job
        except Exception as exc:
            # 走到这里说明导入流程本身崩溃了，而不是单个文件失败，需要额外补一条任务级错误。
            failure_items.append(
                make_failure_item(source_entry, FAILURE_REASON_IMPORT_JOB_CRASHED, str(exc))
            )
            job.imported_count = imported_count
            job.skipped_count = skipped_count
            job.failed_count = failed_count + 1
            job.state = IMPORT_JOB_STATE_FAILED
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.finished_at = utc_now_for_db()
            job.save()
            if download_task is not None:
                download_task.import_status = IMPORT_STATUS_FAILED
                download_task.save()
            logger.exception(
                "Import job crashed job_id={} source_path={} detail={}",
                job.id,
                str(source_entry),
                exc,
            )
            self._emit_progress(
                progress_callback,
                event="job_failed",
                text="媒体导入任务失败",
                summary_patch={
                    "imported_count": imported_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count + 1,
                    "new_playable_movies": list(new_playable_movies.values()),
                },
            )
            raise
