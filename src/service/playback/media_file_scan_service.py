from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import Media
from src.service.catalog.movie_subtitle_service import MovieSubtitleService


@dataclass(frozen=True)
class MediaFileCheckResult:
    id: int
    path: str
    file_exists: bool
    valid_before: bool
    valid_after: bool
    updated: bool
    invalidated: bool
    revived: bool
    checked_at: datetime


class MediaFileScanService:
    DEFAULT_BATCH_SIZE = 100

    @staticmethod
    def _build_candidate_query(last_media_id: int = 0):
        return (
            Media.select(Media)
            .where(Media.id > last_media_id)
            .order_by(Media.id.asc())
        )

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    @staticmethod
    def _is_cloud115_media(media: Media) -> bool:
        from src.model.enums import MediaLibraryBackend

        library = media.library
        return library is not None and library.backend == MediaLibraryBackend.CLOUD115.value

    def _scan_cloud115_media(self, media: Media) -> dict[str, bool | datetime]:
        """cloud115 媒体对账：pickcode_info 探活判存在性。

        - NotFound → 远端已删/封禁 → 标 invalid；重新出现 → 复活。
        - AuthError / 限流 / 网络错 → **跳过本条不动 valid**：cookies 失效不代表文件没了，
          误标 invalid 会让整库在凭据过期期间集体失效。
        """
        import asyncio

        from src.lib.cloud115 import Cloud115Error, Cloud115NotFoundError
        from src.service.cloud115 import cloud115_client_for

        checked_at = utc_now_for_db()
        result = {
            "updated": False,
            "invalidated": False,
            "revived": False,
            "file_exists": bool(media.valid),
            "checked_at": checked_at,
        }
        pickcode = (media.backend_locator or {}).get("pickcode")
        if not pickcode:
            logger.warning("Cloud115 media missing pickcode media_id={}", media.id)
            return result

        async def _probe() -> bool:
            async with cloud115_client_for(media.library) as client:
                await client.pickcode_info(pickcode)
                return True

        try:
            file_exists = asyncio.run(_probe())
        except Cloud115NotFoundError:
            file_exists = False
        except Cloud115Error as exc:
            # 上游不可用：本轮跳过，保持现状（不误标 invalid）。
            logger.warning(
                "Cloud115 media probe skipped media_id={} detail={}", media.id, exc
            )
            return result

        result["file_exists"] = file_exists
        if media.valid != file_exists:
            media.valid = file_exists
            media.updated_at = checked_at
            media.save(only=[Media.valid, Media.updated_at])
            result["updated"] = True
            result["invalidated"] = not file_exists
            result["revived"] = file_exists
        if media.movie_number:
            MovieSubtitleService.sync_movie_subtitles(media.movie)
        return result

    def _scan_single_media(self, media: Media) -> dict[str, bool | datetime]:
        if self._is_cloud115_media(media):
            return self._scan_cloud115_media(media)
        file_path = Path(media.path).expanduser().resolve()
        file_exists = file_path.exists() and file_path.is_file()
        checked_at = utc_now_for_db()
        updates: dict = {}
        result = {
            "updated": False,
            "invalidated": False,
            "revived": False,
            "file_exists": file_exists,
            "checked_at": checked_at,
        }

        if media.valid != file_exists:
            # valid 的语义以当前文件状态为准，巡检要把库里状态修正回来。
            updates[Media.valid] = file_exists
            result["invalidated"] = not file_exists
            result["revived"] = file_exists

        if not updates:
            # 字幕同步是 JAV 影片维度能力，非 JAV 媒体跳过。
            if media.movie_number:
                MovieSubtitleService.sync_movie_subtitles(media.movie)
            return result

        for field, value in updates.items():
            setattr(media, field.name, value)
        media.updated_at = checked_at
        media.save(only=[*updates.keys(), Media.updated_at])
        if media.movie_number:
            MovieSubtitleService.sync_movie_subtitles(media.movie)
        result["updated"] = True
        return result

    def check_media_file(self, media_id: int) -> MediaFileCheckResult:
        media = Media.get_or_none(Media.id == media_id)
        if media is None:
            raise ApiError(
                404,
                "media_not_found",
                "Media not found",
                {"media_id": media_id},
            )

        valid_before = bool(media.valid)
        result = self._scan_single_media(media)
        media = Media.get_by_id(media.id)
        # 单条复查直接返回状态变化，前端无需再推断 valid 是否被本次修正。
        return MediaFileCheckResult(
            id=media.id,
            path=media.display_path,
            file_exists=bool(result["file_exists"]),
            valid_before=valid_before,
            valid_after=bool(media.valid),
            updated=bool(result["updated"]),
            invalidated=bool(result["invalidated"]),
            revived=bool(result["revived"]),
            checked_at=result["checked_at"],
        )

    def scan_media_files(
        self,
        batch_size: int = DEFAULT_BATCH_SIZE,
        progress_callback=None,
    ) -> dict[str, int]:
        stats = {
            "scanned_media": 0,
            "updated_media": 0,
            "skipped_media": 0,
            "failed_media": 0,
            "invalidated_media": 0,
            "revived_media": 0,
        }
        last_media_id = 0
        self._emit_progress(
            progress_callback,
            current=0,
            total=None,
            text="开始巡检媒体文件",
            summary_patch=stats,
        )

        while True:
            candidates = list(self._build_candidate_query(last_media_id).limit(max(1, batch_size)))
            if not candidates:
                return stats

            for media in candidates:
                last_media_id = media.id
                stats["scanned_media"] += 1
                try:
                    result = self._scan_single_media(media)
                except Exception as exc:
                    stats["failed_media"] += 1
                    logger.warning(
                        "Scan media file failed media_id={} path={} detail={}",
                        media.id,
                        media.path,
                        exc,
                    )
                    self._emit_progress(
                        progress_callback,
                        current=stats["scanned_media"],
                        total=None,
                        text=f"媒体文件巡检失败 media_id={media.id}",
                        summary_patch=stats,
                    )
                    continue

                if result["updated"]:
                    stats["updated_media"] += 1
                else:
                    stats["skipped_media"] += 1
                if result["invalidated"]:
                    stats["invalidated_media"] += 1
                if result["revived"]:
                    stats["revived_media"] += 1

                self._emit_progress(
                    progress_callback,
                    current=stats["scanned_media"],
                    total=None,
                    text=f"已巡检媒体文件 media_id={media.id}",
                    summary_patch=stats,
                )
