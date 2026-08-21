"""媒体导入 service。

顶层编排 ``import_from_source``：调 ``media_source_scanner`` 完成扫描分组、并发抓取远端元数据、
再委托 ``media_import_writer`` 完成落库，最终返回统一统计结果。
"""

import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, local
from typing import Any

from loguru import logger
from pydantic import BaseModel

# 导入状态/失败原因的取值统一收口到 media_import_status 模块。
from src.common.media_import_status import (
    FAILURE_REASON_IMAGE_DOWNLOAD_FAILED,
    FAILURE_REASON_MEDIA_IMPORT_FAILED,
    FAILURE_REASON_METADATA_FETCH_FAILED,
    FAILURE_REASON_METADATA_UPSERT_FAILED,
    FAILURE_REASON_NO_MEDIA_FILES_FOUND,
    make_failure_item,
)
from src.common.service_helpers import emit_progress
from src.config.config import settings
from src.model import MediaLibrary, Movie, get_database
from src.model.enums import MediaLibraryBackend
from src.schema.transfers.media_import import ImportResult

# 从子模块而非 src.service.catalog 包导入：走包会执行 catalog/__init__.py，而其中的
# movie_subscription_service 又要导入 transfers 域，形成 catalog <-> transfers 的包级循环，
# 逼得对面只能把所有 transfers 导入塞进函数体。指到具体文件即可绕开 __init__ 的连锁初始化。
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.movie_image_service import ImageDownloadError
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService
from src.service.transfers.imports.source_scanner import (
    ImportTransferMode,
    find_media_library_containing_path,
    scan_source_files,
)
from src.service.transfers.imports.writer import import_single_scanned_file
from src.service.transfers.shared.file_transfer import delete_source_files

ImportProgressCallback = Callable[[dict[str, object]], None]


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
            movie, _created = catalog_import_service.import_movie_if_missing(
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

    @contextmanager
    def metadata_import_batch(
        self,
        movie_numbers: Sequence[str],
        *,
        thread_name_prefix: str = "import-metadata",
    ) -> Iterator[dict[str, Future[MetadataImportResult]]]:
        """统一管理元数据并发池，供本地与云端导入复用，调用方按原顺序消费 Future。"""
        if not movie_numbers:
            yield {}
            return
        with ThreadPoolExecutor(
            max_workers=self._metadata_max_workers(len(movie_numbers)),
            thread_name_prefix=thread_name_prefix,
        ) as executor:
            yield {
                movie_number: executor.submit(self._import_movie_metadata, movie_number)
                for movie_number in movie_numbers
            }

    def import_from_source(
        self,
        source_path: str,
        library_id: int,
        *,
        progress_callback: ImportProgressCallback | None = None,
        transfer_mode: ImportTransferMode = "auto",
    ) -> ImportResult:
        """执行一次完整的本地 JAV 导入并返回统计。"""
        if transfer_mode not in ("auto", "cleanup-source"):
            logger.warning("Import rejected invalid transfer mode transfer_mode={}", transfer_mode)
            raise ValueError("invalid_transfer_mode")

        source_entry = Path(source_path).expanduser().resolve()
        if not source_entry.exists() or (not source_entry.is_dir() and not source_entry.is_file()):
            logger.warning("Import rejected invalid source path source_path={}", source_path)
            raise ValueError("source_path_not_found")

        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            logger.warning("Import rejected because media library not found library_id={}", library_id)
            raise ValueError("media_library_not_found")
        if library.backend != MediaLibraryBackend.LOCAL.value:
            logger.warning(
                "Import rejected because media library backend is not local library_id={} backend={}",
                library_id,
                library.backend,
            )
            raise ValueError("media_library_backend_mismatch")

        if transfer_mode == "cleanup-source":
            matched_library = find_media_library_containing_path(source_entry)
            if matched_library is not None:
                logger.warning(
                    "Import rejected cleanup-source inside media library source_path={} matched_library_id={} matched_library_root={}",
                    str(source_entry),
                    matched_library.id,
                    matched_library.backend_config.get("root_path"),
                )
                raise ValueError("cleanup_source_inside_media_library")

        logger.info(
            "Import start source_path={} library_id={} library_root={}",
            str(source_entry),
            library_id,
            library.backend_config.get("root_path"),
        )
        failure_items: list[dict[str, str]] = []
        imported_count = 0
        skipped_count = 0
        failed_count = 0
        new_playable_movies: dict[int, dict[str, object]] = {}

        try:
            # 第一阶段只扫描和分组文件，不碰远端元数据和目标媒体库。
            # scan 命中已入库文件时，cleanup-source 模式下仍需要清理源目录，走 callback 复用共享删源工具。
            def _cleanup_duplicate_sources(source_paths: list[Path]) -> int:
                return delete_source_files(source_paths, failure_items, transfer_mode=transfer_mode)

            grouped_files, grouped_skipped_count, grouped_failed_count = scan_source_files(
                source_entry,
                failure_items,
                transfer_mode=transfer_mode,
                on_duplicate_source_paths=_cleanup_duplicate_sources,
            )
            skipped_count += grouped_skipped_count
            failed_count += grouped_failed_count
            logger.info(
                "Import scan completed grouped_numbers={} skipped={} failed={}",
                len(grouped_files),
                grouped_skipped_count,
                grouped_failed_count,
            )
            # 空来源不能静默成功，否则下载页会显示“已导入”但没有任何媒体。
            if not grouped_files and grouped_skipped_count == 0 and grouped_failed_count == 0:
                failed_count += 1
                failure_items.append(
                    make_failure_item(
                        source_entry,
                        FAILURE_REASON_NO_MEDIA_FILES_FOUND,
                        "导入来源中没有扫描到可导入的视频",
                    )
                )
            total_movie_numbers = len(grouped_files)
            completed_movie_numbers = 0
            emit_progress(
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

            if grouped_files:
                with self.metadata_import_batch(
                    list(grouped_files),
                    thread_name_prefix="import-metadata",
                ) as metadata_futures:
                    for movie_number, group in grouped_files.items():
                        logger.info(
                            "Import processing movie_number={} files={}",
                            movie_number,
                            len(group.files),
                        )
                        emit_progress(
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
                            emit_progress(
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
                        emit_progress(
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

                        # 多分部（VR/FC2 等）不做合并：每个文件一条 Media，与 cloud115 管线保持一致。
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
                                    "Import media failed movie_number={} source={} detail={}",
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
                        emit_progress(
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

            logger.info(
                "Import finished imported={} skipped={} failed={}",
                imported_count,
                skipped_count,
                failed_count,
            )
            emit_progress(
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
            return ImportResult(
                imported_count=imported_count,
                skipped_count=skipped_count,
                failed_count=failed_count,
                new_playable_movies=list(new_playable_movies.values()),
            )
        except Exception as exc:
            logger.exception(
                "Import crashed source_path={} detail={}",
                str(source_entry),
                exc,
            )
            emit_progress(
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
