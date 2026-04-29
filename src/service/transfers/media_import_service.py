"""媒体导入 service。

负责扫描导入目录、按番号分组、抓取远端元数据、导入媒体文件，并维护 ImportJob/DownloadTask 状态。
阅读入口建议从 ``import_from_source`` 开始，再看扫描、指纹计算和单文件落库 helper。
"""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import ffmpy
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock, local
from typing import Any, Callable, Dict, List, Literal, Tuple

from loguru import logger

from src.common import parse_movie_number_from_path
from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.model import DownloadTask, ImportJob, Media, MediaLibrary, Movie, get_database
from src.service.catalog import CatalogImportService, ImageDownloadError
from src.service.catalog.movie_subtitle_service import MovieSubtitleService
from src.service.playback.media_thumbnail_service import MediaThumbnailService
from src.service.playback.media_metadata_probe_service import (
    MediaMetadataProbeService,
)
from src.service.system.resource_task_state_service import ResourceTaskStateService
from src.service.transfers.tag_rules import build_media_special_tags

SUPPORTED_VIDEO_EXTENSIONS = frozenset(
    {
        ".m2ts",
        ".mkv",
        ".mp4",
    }
)
IMPORTED_SIDECAR_SUBTITLE_EXTENSION = ".srt"


def parse_movie_number(file_path: str) -> str:
    return parse_movie_number_from_path(file_path)


ImportProgressCallback = Callable[[Dict[str, object]], None]


@dataclass
class MetadataImportResult:
    movie_number: str
    movie_id: int | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None


@dataclass(frozen=True)
class ScannedSourceFile:
    path: Path
    content_fingerprint: str
    subtitle_path: Path | None = None


@dataclass
class ImportGroup:
    movie_number: str
    files: List[ScannedSourceFile]
    merge_mode: Literal["single", "vr_concat"]


class MediaImportService:
    """把待导入目录中的视频文件转换为本地媒体库记录。"""

    CONTENT_FINGERPRINT_VERSION = "fingerprint-v1"
    FULL_HASH_THRESHOLD_BYTES = 100 * 1024 * 1024
    SAMPLE_WINDOW_BYTES = 5 * 1024 * 1024
    INTERIOR_SAMPLE_COUNT = 6
    HASH_READ_CHUNK_BYTES = 1024 * 1024

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
                failure_reason="metadata_fetch_failed",
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
                failure_reason="image_download_failed",
                failure_detail=str(exc),
            )
        except Exception as exc:
            logger.exception("Import metadata upsert failed movie_number={} detail={}", movie_number, exc)
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason="metadata_upsert_failed",
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
    ) -> ImportJob:
        """执行一次完整的媒体导入，并把中间状态写回 ImportJob。"""
        source_entry = Path(source_path).expanduser().resolve()
        if not source_entry.exists() or (not source_entry.is_dir() and not source_entry.is_file()):
            logger.warning("Import rejected invalid source path source_path={}", source_path)
            raise ValueError("source_path_not_found")

        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            logger.warning("Import rejected because media library not found library_id={}", library_id)
            raise ValueError("media_library_not_found")

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
                state="pending",
            )
        else:
            job = ImportJob.get_by_id(import_job_id)
            job.source_path = str(source_entry)
            job.library = library
            job.download_task = download_task
            job.state = "pending"
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

        job.state = "running"
        job.started_at = utc_now_for_db()
        job.save()
        if download_task is not None:
            download_task.import_status = "running"
            download_task.save()
        logger.info("Import job running job_id={}", job.id)

        try:
            # 第一阶段只扫描和分组文件，不碰远端元数据和目标媒体库。
            grouped_files, grouped_skipped_count, grouped_failed_count = self._scan_source_files(
                source_entry,
                failure_items,
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
                                    {
                                        "path": str(file_entry.path),
                                        "reason": metadata_result.failure_reason,
                                        "detail": metadata_result.failure_detail or "",
                                    }
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
                                if self._import_vr_media_group(group=group, library=library, movie=movie, failure_items=failure_items):
                                    imported_count += 1
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
                                    {
                                        "path": str(group.files[0].path),
                                        "reason": "vr_media_merge_failed",
                                        "detail": str(exc),
                                    }
                                )
                        else:
                            for file_entry in group.files:
                                try:
                                    if self._import_single_scanned_file(
                                        file_entry=file_entry,
                                        library=library,
                                        movie=movie,
                                    ):
                                        imported_count += 1
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
                                        {
                                            "path": str(file_entry.path),
                                            "reason": "media_import_failed",
                                            "detail": str(exc),
                                        }
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
            job.state = "failed" if failed_count > 0 else "completed"
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.finished_at = utc_now_for_db()
            job.save()
            if download_task is not None:
                download_task.import_status = "failed" if failed_count > 0 else "completed"
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
                {
                    "path": str(source_entry),
                    "reason": "import_job_crashed",
                    "detail": str(exc),
                }
            )
            job.imported_count = imported_count
            job.skipped_count = skipped_count
            job.failed_count = failed_count + 1
            job.state = "failed"
            job.failed_files = json.dumps(failure_items, ensure_ascii=False)
            job.finished_at = utc_now_for_db()
            job.save()
            if download_task is not None:
                download_task.import_status = "failed"
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

    def _scan_source_files(
        self,
        source_entry: Path,
        failure_items: List[Dict[str, str]],
    ) -> Tuple[Dict[str, ImportGroup], int, int]:
        """扫描源目录，过滤无效文件，并按影片编号聚合待导入媒体。"""
        minimum_size = settings.media.allowed_min_video_file_size
        grouped_candidates: Dict[str, List[ScannedSourceFile]] = {}
        skipped_count = 0
        failed_count = 0
        scanned_count = 0
        media_candidate_count = 0

        logger.info(
            "Import scan start source_path={} media_types={} min_size_bytes={}",
            str(source_entry),
            sorted(list(SUPPORTED_VIDEO_EXTENSIONS)),
            minimum_size,
        )

        candidate_paths = [source_entry] if source_entry.is_file() else sorted(source_entry.rglob("*"))
        for path in candidate_paths:
            if not path.is_file():
                continue
            scanned_count += 1
            if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
                continue
            media_candidate_count += 1

            # 小文件通常是样本、字幕或下载残片，直接记为 skipped，不进入后续元数据流程。
            file_size = path.stat().st_size
            if file_size < minimum_size:
                skipped_count += 1
                logger.warning(
                    "Import scan skip small file path={} size_bytes={} min_size_bytes={}",
                    str(path),
                    file_size,
                    minimum_size,
                )
                failure_items.append(
                    {
                        "path": str(path),
                        "reason": "file_too_small",
                    }
                )
                continue

            movie_number = parse_movie_number(str(path))
            if not movie_number:
                failed_count += 1
                logger.warning("Import scan failed to parse movie number path={}", str(path))
                failure_items.append(
                    {
                        "path": str(path),
                        "reason": "movie_number_not_found",
                    }
                )
                continue

            # 指纹里带上归一化后的番号，既能识别同内容重复文件，也能避免不同影片同尺寸文件误撞。
            content_fingerprint = self._build_content_fingerprint(path, movie_number)
            existing_media = self._find_media_by_content_fingerprint(content_fingerprint, valid=True)
            if existing_media is not None:
                skipped_count += 1
                logger.info(
                    "Import media duplicate ignored movie_number={} source={} existing_media_id={} existing_media_path={} content_fingerprint={}",
                    movie_number,
                    str(path),
                    existing_media.id,
                    existing_media.path,
                    content_fingerprint,
                )
                continue

            subtitle_path = self._find_sidecar_subtitle(path)
            if movie_number not in grouped_candidates:
                grouped_candidates[movie_number] = []
            grouped_candidates[movie_number].append(
                ScannedSourceFile(
                    path=path,
                    content_fingerprint=content_fingerprint,
                    subtitle_path=subtitle_path,
                )
            )
            logger.info("Import scan grouped file path={} movie_number={}", str(path), movie_number)

        grouped_files: Dict[str, ImportGroup] = {}
        for movie_number, file_entries in grouped_candidates.items():
            original_file_count = len(file_entries)
            deduplicated_entries: List[ScannedSourceFile] = []
            seen_fingerprints: set[str] = set()
            for file_entry in sorted(file_entries, key=lambda item: item.path.name):
                if file_entry.content_fingerprint in seen_fingerprints:
                    skipped_count += 1
                    logger.info(
                        "Import grouped duplicate ignored movie_number={} source={} content_fingerprint={}",
                        movie_number,
                        str(file_entry.path),
                        file_entry.content_fingerprint,
                    )
                    continue
                deduplicated_entries.append(file_entry)
                seen_fingerprints.add(file_entry.content_fingerprint)

            merge_mode: Literal["single", "vr_concat"] = "single"
            if original_file_count > 1 and self._group_is_vr(movie_number, deduplicated_entries):
                merge_mode = "vr_concat"
            grouped_files[movie_number] = ImportGroup(
                movie_number=movie_number,
                files=deduplicated_entries,
                merge_mode=merge_mode,
            )

        logger.info(
            "Import scan summary source_path={} scanned_files={} media_candidates={} grouped_numbers={} skipped={} failed={}",
            str(source_entry),
            scanned_count,
            media_candidate_count,
            len(grouped_files),
            skipped_count,
            failed_count,
        )

        return grouped_files, skipped_count, failed_count

    def _group_is_vr(self, movie_number: str, files: List[ScannedSourceFile]) -> bool:
        if "VR" in movie_number.upper():
            return True
        return any("VR" in file_entry.path.name.upper() for file_entry in files)

    def _build_content_fingerprint(self, file_path: Path, movie_number: str) -> str:
        """构建内容指纹。

        小文件直接全量 hash，大文件只抽样多个区段，避免导入时对超大视频做整文件扫描。
        """
        resolved_path = file_path.expanduser().resolve()
        file_size = resolved_path.stat().st_size
        hasher = hashlib.sha256()
        hasher.update(f"{self.CONTENT_FINGERPRINT_VERSION}\0".encode("utf-8"))
        hasher.update(f"{file_size}\0".encode("utf-8"))
        hasher.update(f"{self._normalize_movie_number(movie_number)}\0".encode("utf-8"))

        if file_size <= self.FULL_HASH_THRESHOLD_BYTES:
            self._update_hash_with_range(hasher, resolved_path, 0, file_size)
        else:
            for start, end in self._sample_ranges(file_size):
                self._update_hash_with_range(hasher, resolved_path, start, end)

        return hasher.hexdigest()

    @staticmethod
    def _build_group_content_fingerprint(content_fingerprints: List[str]) -> str:
        hasher = hashlib.sha256()
        hasher.update("\0".join(content_fingerprints).encode("utf-8"))
        return hasher.hexdigest()

    @staticmethod
    def _normalize_movie_number(movie_number: str) -> str:
        return movie_number.strip().upper()

    def _sample_ranges(self, file_size: int) -> List[Tuple[int, int]]:
        """为大文件生成首尾加中间若干窗口的抽样区间。"""
        ranges: List[Tuple[int, int]] = [
            (0, min(file_size, self.SAMPLE_WINDOW_BYTES)),
            (max(0, file_size - self.SAMPLE_WINDOW_BYTES), file_size),
        ]
        half_window = self.SAMPLE_WINDOW_BYTES // 2
        for index in range(1, self.INTERIOR_SAMPLE_COUNT + 1):
            center = int(file_size * index / (self.INTERIOR_SAMPLE_COUNT + 1))
            start = max(0, center - half_window)
            end = min(file_size, start + self.SAMPLE_WINDOW_BYTES)
            start = max(0, end - self.SAMPLE_WINDOW_BYTES)
            ranges.append((start, end))
        return self._merge_ranges(ranges)

    @staticmethod
    def _merge_ranges(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
        """合并重叠采样区间，避免重复读取同一段文件。"""
        merged: List[Tuple[int, int]] = []
        for start, end in sorted(ranges):
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
                continue
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        return merged

    def _update_hash_with_range(
        self,
        hasher,
        file_path: Path,
        start: int,
        end: int,
    ) -> None:
        """把指定文件区间增量写入哈希器。"""
        remaining = max(0, end - start)
        if remaining == 0:
            return
        with file_path.open("rb") as file_handle:
            file_handle.seek(start)
            while remaining > 0:
                chunk = file_handle.read(min(self.HASH_READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                hasher.update(chunk)
                remaining -= len(chunk)

    @staticmethod
    def _find_media_by_content_fingerprint(content_fingerprint: str, *, valid: bool) -> Media | None:
        """按内容指纹查找最新一条指定有效状态的媒体记录。"""
        query = (
            Media.select()
            .where(
                Media.content_fingerprint == content_fingerprint,
                Media.valid == valid,
            )
            .order_by(Media.id.desc())
        )
        return query.first()

    def _import_single_scanned_file(
        self,
        *,
        file_entry: ScannedSourceFile,
        library: MediaLibrary,
        movie: Movie,
    ) -> bool:
        existing_media = self._find_media_by_content_fingerprint(
            file_entry.content_fingerprint,
            valid=True,
        )
        if existing_media is not None:
            logger.info(
                "Import media duplicate ignored movie_number={} source={} existing_media_id={} existing_media_path={} content_fingerprint={}",
                movie.movie_number,
                str(file_entry.path),
                existing_media.id,
                existing_media.path,
                file_entry.content_fingerprint,
            )
            return False

        storage_mode, target_path = self._import_single_media_file(
            file_path=file_entry.path,
            library=library,
            movie_number=movie.movie_number,
        )
        self._import_sidecar_subtitle(file_entry.path, target_path)
        file_size = file_entry.path.stat().st_size
        self._upsert_media(
            movie=movie,
            library=library,
            target_path=target_path,
            storage_mode=storage_mode,
            content_fingerprint=file_entry.content_fingerprint,
            file_size=file_size,
            special_tag_source_paths=[file_entry.path],
            has_sidecar_subtitle=file_entry.subtitle_path is not None,
        )
        MovieSubtitleService.sync_movie_subtitles(movie)
        logger.info(
            "Import media success movie_number={} source={} target={} storage_mode={}",
            movie.movie_number,
            str(file_entry.path),
            str(target_path),
            storage_mode,
        )
        return True

    def _import_vr_media_group(
        self,
        *,
        group: ImportGroup,
        library: MediaLibrary,
        movie: Movie,
        failure_items: List[Dict[str, str]],
    ) -> bool:
        group_fingerprint = self._build_group_content_fingerprint(
            [file_entry.content_fingerprint for file_entry in group.files]
        )
        existing_media = self._find_media_by_content_fingerprint(group_fingerprint, valid=True)
        if existing_media is not None:
            logger.info(
                "Import VR media group duplicate ignored movie_number={} source={} existing_media_id={} existing_media_path={} content_fingerprint={}",
                movie.movie_number,
                ",".join(str(file_entry.path) for file_entry in group.files),
                existing_media.id,
                existing_media.path,
                group_fingerprint,
            )
            return False

        target_directory = self._create_version_directory(
            Path(library.root_path).expanduser(),
            movie.movie_number,
        )
        target_path = target_directory / f"{movie.movie_number}{group.files[0].path.suffix.lower()}"
        self._merge_media_files(group.files, target_path)

        subtitle_path, multiple_subtitles = self._select_group_subtitle(group)
        if subtitle_path is not None:
            self._transfer_file(subtitle_path, target_path.with_suffix(IMPORTED_SIDECAR_SUBTITLE_EXTENSION))
        elif multiple_subtitles:
            failure_items.append(
                {
                    "path": str(group.files[0].path),
                    "reason": "merge_subtitle_skipped_multiple_sidecars",
                }
            )

        self._upsert_media(
            movie=movie,
            library=library,
            target_path=target_path,
            storage_mode="concat",
            content_fingerprint=group_fingerprint,
            file_size=target_path.stat().st_size,
            special_tag_source_paths=[file_entry.path for file_entry in group.files],
            has_sidecar_subtitle=subtitle_path is not None,
        )
        MovieSubtitleService.sync_movie_subtitles(movie)
        logger.info(
            "Import VR media group success movie_number={} sources={} target={} content_fingerprint={}",
            movie.movie_number,
            ",".join(str(file_entry.path) for file_entry in group.files),
            str(target_path),
            group_fingerprint,
        )
        return True

    def _upsert_media(
        self,
        *,
        movie: Movie,
        library: MediaLibrary,
        target_path: Path,
        storage_mode: str,
        content_fingerprint: str,
        file_size: int,
        special_tag_source_paths: List[Path],
        has_sidecar_subtitle: bool,
    ) -> None:
        if file_size <= 0:
            try:
                file_size = target_path.stat().st_size
            except (FileNotFoundError, OSError):
                file_size = 0
        metadata = self.media_metadata_probe_service.probe_file(target_path)
        resolution = metadata.resolution
        duration_seconds = metadata.duration_seconds if metadata.duration_seconds > 0 else 0
        invalid_media = self._find_media_by_content_fingerprint(
            content_fingerprint,
            valid=False,
        )
        effective_video_info = metadata.video_info
        if invalid_media is not None and effective_video_info is None:
            effective_video_info = invalid_media.video_info
        special_tags = build_media_special_tags(
            [str(path) for path in special_tag_source_paths],
            movie.movie_number,
            video_info=effective_video_info,
            has_subtitle=has_sidecar_subtitle,
        )
        if invalid_media is None:
            media = Media.create(
                movie=movie,
                library=library,
                path=str(target_path),
                storage_mode=storage_mode,
                content_fingerprint=content_fingerprint,
                file_size_bytes=file_size,
                resolution=resolution,
                duration_seconds=duration_seconds,
                video_info=effective_video_info,
                special_tags=special_tags,
                valid=True,
            )
            self._reset_thumbnail_generation_state(media.id)
            return

        invalid_media.movie = movie
        invalid_media.library = library
        invalid_media.path = str(target_path)
        invalid_media.storage_mode = storage_mode
        invalid_media.content_fingerprint = content_fingerprint
        invalid_media.file_size_bytes = file_size
        if resolution is not None:
            invalid_media.resolution = resolution
        if duration_seconds > 0:
            invalid_media.duration_seconds = duration_seconds
        if metadata.video_info is not None:
            invalid_media.video_info = metadata.video_info
        invalid_media.special_tags = special_tags
        invalid_media.valid = True
        invalid_media.updated_at = utc_now_for_db()
        invalid_media.save()
        self._reset_thumbnail_generation_state(invalid_media.id)

    @staticmethod
    def _reset_thumbnail_generation_state(media_id: int) -> None:
        # 导入新文件或复活旧媒体后，缩略图任务必须回到全新的待处理状态。
        ResourceTaskStateService.reset_for_requeue(
            MediaThumbnailService.TASK_KEY,
            media_id,
        )

    def _import_single_media_file(
        self,
        file_path: Path,
        library: MediaLibrary,
        movie_number: str,
    ) -> Tuple[str, Path]:
        """为单个媒体文件创建目标版本目录并完成文件传输。"""
        library_root = Path(library.root_path).expanduser()
        target_directory = self._create_version_directory(library_root, movie_number)
        target_filename = f"{movie_number}{file_path.suffix.lower()}"
        target_path = target_directory / target_filename
        storage_mode = self._transfer_file(file_path, target_path)
        return storage_mode, target_path

    def _import_sidecar_subtitle(self, source_video_path: Path, target_video_path: Path) -> None:
        subtitle_path = self._find_sidecar_subtitle(source_video_path)
        if subtitle_path is None:
            return
        target_subtitle_path = target_video_path.with_suffix(IMPORTED_SIDECAR_SUBTITLE_EXTENSION)
        self._transfer_file(subtitle_path, target_subtitle_path)

    @staticmethod
    def _select_group_subtitle(group: ImportGroup) -> Tuple[Path | None, bool]:
        subtitles = []
        for file_entry in group.files:
            if file_entry.subtitle_path is None:
                continue
            if file_entry.subtitle_path not in subtitles:
                subtitles.append(file_entry.subtitle_path)
        if len(subtitles) == 1:
            return subtitles[0], False
        if len(subtitles) > 1:
            return None, True
        return None, False

    @staticmethod
    def _find_sidecar_subtitle(source_video_path: Path) -> Path | None:
        source_directory = source_video_path.parent
        source_stem = source_video_path.stem
        for path in sorted(source_directory.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file():
                continue
            if path.stem != source_stem or path.suffix.lower() != IMPORTED_SIDECAR_SUBTITLE_EXTENSION:
                continue
            return path
        return None

    def _create_version_directory(self, library_root: Path, movie_number: str) -> Path:
        """在影片目录下创建唯一版本子目录，避免重复导入时相互覆盖。"""
        number_directory = library_root / movie_number
        number_directory.mkdir(parents=True, exist_ok=True)

        base_version = str(self.now_ms())
        version = base_version
        suffix = 1
        while (number_directory / version).exists():
            version = f"{base_version}-{suffix}"
            suffix += 1

        target_directory = number_directory / version
        target_directory.mkdir(parents=True, exist_ok=False)
        logger.debug("Import version directory created movie_number={} version_dir={}", movie_number, str(target_directory))
        return target_directory

    def _transfer_file(self, source_path: Path, target_path: Path) -> str:
        """优先硬链接，失败时回退复制，并返回实际存储模式。"""
        try:
            os.link(source_path, target_path)
            logger.debug("Import transfer hardlink source={} target={}", str(source_path), str(target_path))
            return "hardlink"
        except OSError as exc:
            logger.warning(
                "Import transfer hardlink failed, fallback to copy source={} target={} detail={}",
                str(source_path),
                str(target_path),
                exc,
            )
            shutil.copy2(source_path, target_path)
            logger.debug("Import transfer copied source={} target={}", str(source_path), str(target_path))
            return "copy"

    def _merge_media_files(self, files: List[ScannedSourceFile], target_path: Path) -> None:
        ordered_files = sorted(files, key=lambda item: item.path.name)
        with NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as temp_file:
            temp_file.write("\n".join([f"file '{str(file_entry.path)}'" for file_entry in ordered_files]))
            temp_file_path = Path(temp_file.name)

        try:
            ffmpeg = ffmpy.FFmpeg(
                global_options=["-f", "concat", "-safe", "0"],
                inputs={str(temp_file_path): None},
                outputs={str(target_path): ["-c", "copy"]},
            )
            ffmpeg.run()
            output_size = target_path.stat().st_size if target_path.exists() else 0
            total_input_size = sum(file_entry.path.stat().st_size for file_entry in ordered_files)
            if (
                output_size <= 0
                or output_size < max(file_entry.path.stat().st_size for file_entry in ordered_files)
                or output_size < int(total_input_size * 0.8)
            ):
                raise RuntimeError("merged_file_size_invalid")
        except Exception:
            if target_path.exists():
                target_path.unlink()
            raise
        finally:
            if temp_file_path.exists():
                temp_file_path.unlink()
