"""手动字幕目录导入 service。

用户把按番号命名的 .srt 放进浏览白名单内的目录后，后台递归扫描：
从文件名解析番号 -> 查影片 -> 归档到 ``movies/<shard>/<番号>/subtitles/<番号>-<N>.srt``
并登记 ``Subtitle``。解析不出番号 / 影片不存在 / 文件搬运登记异常进入作业失败列表；
同一影片已存在相同内容时按 ``duplicate_fingerprint`` 语义跳过。源文件始终保留（不删源）。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.fs_browse import is_within_allowed_roots
from src.common.media_import_status import (
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND,
    FAILURE_REASON_NO_SUBTITLE_FILES_FOUND,
    FAILURE_REASON_RETRY_SOURCES_MISSING,
    FAILURE_REASON_SUBTITLE_IMPORT_FAILED,
    FAILURE_REASON_SUBTITLE_MOVIE_NOT_FOUND,
    IMPORT_JOB_STATE_RUNNING,
    make_failure_item,
)
from src.common.movie_numbers import parse_movie_number_from_text
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import emit_progress, find_movie_by_number
from src.common.subtitle_paths import ensure_movie_subtitle_path
from src.config.config import settings
from src.model import Subtitle, SubtitleImportJob, get_database
from src.service.transfers.imports.writer import prepare_movie_subtitle_target_path
from src.service.transfers.shared.file_transfer import transfer_file
from src.service.transfers.shared.import_job_state import finalize_import_job


class SubtitleImportService:
    def import_subtitles_from_source(
        self,
        source_path: str,
        subtitle_import_job_id: int,
        *,
        progress_callback=None,
        only_files: list[str] | None = None,
    ) -> SubtitleImportJob:
        """执行一次完整的手动字幕导入，并把中间状态写回作业行。

        ``only_files`` 提供时仅导入这些绝对路径，用于失败文件的子集重导。
        """
        database = get_database()
        if database.is_closed():
            database.connect()

        source_entry = Path(source_path).expanduser().resolve()
        if not source_entry.exists() or (
            not source_entry.is_dir() and not source_entry.is_file()
        ):
            logger.warning(
                "Subtitle import rejected invalid source path source_path={}", source_path
            )
            raise ValueError("source_path_not_found")

        # 子集重导时把目标文件归一化为绝对路径集合，扫描阶段据此过滤。
        only_file_set: set[str] | None = None
        if only_files is not None:
            only_file_set = {str(Path(item).expanduser().resolve()) for item in only_files}

        job = SubtitleImportJob.get_or_none(SubtitleImportJob.id == subtitle_import_job_id)
        if job is None:
            raise ValueError("subtitle_import_job_not_found")
        job.state = IMPORT_JOB_STATE_RUNNING
        job.started_at = job.started_at or utc_now_for_db()
        job.imported_count = 0
        job.skipped_count = 0
        job.failed_count = 0
        job.failed_files = "[]"
        job.save()

        candidate_paths = self._iter_subtitle_paths(source_entry)
        total = len(candidate_paths)
        imported_count = 0
        skipped_count = 0
        failed_count = 0
        processed_count = 0
        failure_items: list[dict[str, str]] = []
        # 每个影片已归档字幕的内容指纹缓存：同一影片内容相同的字幕只导入一份。
        existing_hashes: dict[int, set[str]] = {}

        logger.info(
            "Subtitle import scan start source_path={} subtitle_files={}",
            str(source_entry),
            total,
        )
        emit_progress(
            progress_callback,
            current=0,
            total=total,
            text="开始扫描字幕文件",
        )

        for subtitle_path in candidate_paths:
            if only_file_set is not None and str(subtitle_path) not in only_file_set:
                continue
            processed_count += 1
            try:
                status, reason, detail = self._import_single_subtitle(
                    subtitle_path,
                    existing_hashes=existing_hashes,
                )
            except Exception as exc:
                status, reason, detail = "failed", FAILURE_REASON_SUBTITLE_IMPORT_FAILED, str(exc)
                logger.exception(
                    "Subtitle import crashed source={} detail={}", str(subtitle_path), exc
                )

            if status == "imported":
                imported_count += 1
            elif status == "skipped":
                skipped_count += 1
                failure_items.append(
                    make_failure_item(subtitle_path, reason, detail)
                )
            else:
                failed_count += 1
                failure_items.append(
                    make_failure_item(subtitle_path, reason, detail)
                )

            emit_progress(
                progress_callback,
                current=processed_count,
                total=total,
                text=f"正在导入字幕 {subtitle_path.name}",
                summary_patch={
                    "imported_count": imported_count,
                    "skipped_count": skipped_count,
                    "failed_count": failed_count,
                },
            )

        # 空目录（或重导时全部源文件已不存在）给用户一个可解释的任务级失败，
        # 避免"completed 零产出"让前端看不出问题。
        if total == 0:
            failure_items.append(
                make_failure_item(source_entry, FAILURE_REASON_NO_SUBTITLE_FILES_FOUND)
            )
            failed_count = max(failed_count, 1)
        elif only_file_set is not None and processed_count == 0:
            failure_items.append(
                make_failure_item(source_entry, FAILURE_REASON_RETRY_SOURCES_MISSING)
            )
            failed_count = max(failed_count, 1)

        # 与媒体导入口径一致：存在单文件失败或任务级失败才判 failed，跳过项不影响终态。
        finalize_import_job(
            job,
            imported_count=imported_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            failure_items=failure_items,
        )

        logger.info(
            "Subtitle import finished job_id={} source_path={} imported={} skipped={} failed={}",
            job.id,
            str(source_entry),
            imported_count,
            skipped_count,
            failed_count,
        )
        emit_progress(
            progress_callback,
            current=processed_count,
            total=total,
            text="字幕导入完成",
            summary_patch={
                "imported_count": imported_count,
                "skipped_count": skipped_count,
                "failed_count": failed_count,
            },
        )
        return job

    def _import_single_subtitle(
        self,
        subtitle_path: Path,
        *,
        existing_hashes: dict[int, set[str]],
    ) -> tuple[str, str, str]:
        """处理单个字幕文件，返回 ``(status, reason, detail)``。

        status 取值：imported（已导入）/ skipped（内容重复主动跳过）/ failed（失败）。
        """
        movie_number = parse_movie_number_from_text(subtitle_path.name)
        if not movie_number:
            return "failed", FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND, ""

        movie = find_movie_by_number(movie_number)
        if movie is None:
            return "failed", FAILURE_REASON_SUBTITLE_MOVIE_NOT_FOUND, movie_number

        content_hash = self._sha256(subtitle_path)
        if content_hash in self._movie_subtitle_hashes(movie, existing_hashes):
            return "skipped", FAILURE_REASON_DUPLICATE_FINGERPRINT, subtitle_path.name

        try:
            # 统一落新布局并分配 <番号>-<N>.srt；硬链接优先、失败回退复制，不删源。
            target_path = prepare_movie_subtitle_target_path(movie.movie_number, None)
            transfer_file(subtitle_path, target_path, transfer_mode="auto")
            Subtitle.create(movie=movie, file_path=str(target_path))
        except Exception as exc:
            logger.exception(
                "Subtitle import failed source={} movie_number={}",
                str(subtitle_path),
                movie.movie_number,
            )
            return "failed", FAILURE_REASON_SUBTITLE_IMPORT_FAILED, str(exc)

        existing_hashes.setdefault(movie.id, set()).add(content_hash)
        logger.info(
            "Subtitle imported source={} movie_number={} target={}",
            str(subtitle_path),
            movie.movie_number,
            str(target_path),
        )
        return "imported", "", ""

    @classmethod
    def _iter_subtitle_paths(cls, source_entry: Path) -> list[Path]:
        """递归枚举目录（或单个文件）下的 .srt，并做浏览白名单兜底校验。"""
        if source_entry.is_file():
            resolved = source_entry.expanduser().resolve()
            if resolved.suffix.lower() != ".srt":
                return []
            return [resolved] if cls._within_allowed_roots(resolved) else []

        candidate_paths = sorted(
            (path for path in source_entry.rglob("*") if path.is_file()),
            key=lambda item: str(item).lower(),
        )
        safe_paths: list[Path] = []
        for path in candidate_paths:
            resolved = path.expanduser().resolve()
            if resolved.suffix.lower() != ".srt":
                continue
            if not cls._within_allowed_roots(resolved):
                logger.warning(
                    "Subtitle scan skip outside browse roots path={}", str(resolved)
                )
                continue
            safe_paths.append(resolved)
        return safe_paths

    @staticmethod
    def _within_allowed_roots(path: Path) -> bool:
        # 符号链接可能指向白名单外，逐文件解析后兜底校验，与浏览/导入链路保持一致。
        return is_within_allowed_roots(path, settings.media_import.browse_roots)

    @staticmethod
    def _movie_subtitle_hashes(
        movie,
        existing_hashes: dict[int, set[str]],
    ) -> set[str]:
        """返回该影片已登记字幕的内容指纹集合（含新老布局合法路径）。"""
        if movie.id in existing_hashes:
            return existing_hashes[movie.id]
        hashes: set[str] = set()
        for subtitle in Subtitle.select().where(Subtitle.movie == movie):
            try:
                absolute_path = ensure_movie_subtitle_path(movie, subtitle.file_path)
            except ApiError:
                continue
            if absolute_path.is_file():
                hashes.add(SubtitleImportService._sha256(absolute_path))
        existing_hashes[movie.id] = hashes
        return hashes

    @staticmethod
    def _sha256(file_path: Path) -> str:
        # 字幕文件很小，分块读保证任意体积都可用且内存恒定。
        digest = hashlib.sha256()
        with open(file_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
