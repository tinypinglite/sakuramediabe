"""手动字幕目录导入 service。

用户把按番号命名的 .srt 放进浏览白名单内的目录后，后台递归扫描：
从文件名解析番号 -> 查影片 -> 归档到 ``movies/<shard>/<番号>/subtitles/<番号>-<N>.srt``
并登记 ``Subtitle``。单文件失败只记日志与 TaskRun 计数；同一影片已存在相同内容时按
``duplicate_fingerprint`` 语义跳过。源文件始终保留（不删源）。
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.fs_browse import (
    assert_within_allowed_roots,
    is_within_allowed_roots,
    normalize_abs_path,
)
from src.common.media_import_status import (
    FAILURE_REASON_DUPLICATE_FINGERPRINT,
    FAILURE_REASON_MOVIE_NUMBER_NOT_FOUND,
    FAILURE_REASON_SUBTITLE_IMPORT_FAILED,
    FAILURE_REASON_SUBTITLE_MOVIE_NOT_FOUND,
)
from src.common.movie_numbers import parse_movie_number_from_text
from src.common.service_helpers import emit_progress, find_movie_by_number
from src.config.config import settings
from src.schema.system.jobs import ManualJobTriggerResponse
from src.service.catalog.subtitle_asset_service import SubtitleAssetService
from src.service.system.task_queue_service import (
    TaskQueueConflictError,
    TaskQueueService,
)


class SubtitleImportService:
    TASK_KEY = "subtitle_directory_import"

    @classmethod
    def trigger_directory_import(cls, source_path: str) -> ManualJobTriggerResponse:
        """发布一条可跨进程领取的 TaskRun，路径留待 worker 执行时严格校验。"""
        try:
            task_run = TaskQueueService.enqueue(
                task_key=cls.TASK_KEY,
                task_name="字幕目录导入",
                trigger_type="manual",
                params={"source_path": source_path},
                conflict="raise",
            )
        except TaskQueueConflictError as exc:
            raise ApiError(
                409,
                "subtitle_import_conflict",
                "已有字幕导入任务在排队或执行",
                {"blocking_task_run_id": exc.blocking_task_run_id},
            ) from exc
        if task_run is None:
            raise RuntimeError("subtitle_import_enqueue_returned_none")
        return ManualJobTriggerResponse(
            task_run_id=task_run.id,
            task_key=task_run.task_key,
            state=task_run.state,
        )

    def import_subtitles_from_source(
        self,
        source_path: str,
        *,
        progress_callback=None,
    ) -> dict[str, int]:
        """执行一次完整扫描；扫描完成即返回统计，不再维护字幕作业副本。"""
        source_entry = normalize_abs_path(source_path)
        if not source_entry.is_dir() and not source_entry.is_file():
            raise ValueError("source_path_not_file_or_directory")
        assert_within_allowed_roots(source_entry, settings.media_import.browse_roots)

        candidate_paths = self._iter_subtitle_paths(source_entry)
        total = len(candidate_paths)
        imported_count = 0
        skipped_count = 0
        failed_count = 0
        processed_count = 0
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
            processed_count += 1
            try:
                status, reason, detail = self._import_single_subtitle(
                    subtitle_path,
                    existing_hashes=existing_hashes,
                )
            except Exception as exc:
                status, reason, detail = (
                    "failed",
                    FAILURE_REASON_SUBTITLE_IMPORT_FAILED,
                    str(exc),
                )
                logger.exception(
                    "Subtitle import crashed source={} detail={}",
                    str(subtitle_path),
                    exc,
                )

            if status == "imported":
                imported_count += 1
            elif status == "skipped":
                skipped_count += 1
            else:
                failed_count += 1
                # 失败文件不再持久化路径清单；保留精准日志供排查，任务仅汇总计数。
                logger.warning(
                    "Subtitle import item failed source={} reason={} detail={}",
                    str(subtitle_path),
                    reason,
                    detail,
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

        logger.info(
            "Subtitle import finished source_path={} imported={} skipped={} failed={}",
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
        return {
            "imported_count": imported_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
        }

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

        try:
            # 统一走共享资产 service：内容指纹去重 + 落新布局 + Subtitle 登记。
            status, _reason, detail = SubtitleAssetService.register_subtitle_file(
                movie,
                subtitle_path,
                existing_hashes=existing_hashes,
            )
            if status == "imported":
                logger.info(
                    "Subtitle imported source={} movie_number={} target={}",
                    str(subtitle_path),
                    movie.movie_number,
                    detail,
                )
                return status, "", ""
            if status == "skipped":
                return status, FAILURE_REASON_DUPLICATE_FINGERPRINT, subtitle_path.name
            return "failed", FAILURE_REASON_SUBTITLE_IMPORT_FAILED, detail
        except Exception as exc:
            logger.exception(
                "Subtitle import failed source={} movie_number={}",
                str(subtitle_path),
                movie.movie_number,
            )
            return "failed", FAILURE_REASON_SUBTITLE_IMPORT_FAILED, str(exc)

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
        """返回该影片已登记字幕的内容指纹集合。"""
        if movie.id in existing_hashes:
            return existing_hashes[movie.id]
        hashes = SubtitleAssetService.movie_subtitle_hashes(movie)
        existing_hashes[movie.id] = hashes
        return hashes
