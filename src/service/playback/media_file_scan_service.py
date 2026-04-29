from pathlib import Path

from loguru import logger

from src.common.runtime_time import utc_now_for_db
from src.model import Media
from src.service.catalog.movie_subtitle_service import MovieSubtitleService
from src.service.playback.media_metadata_probe_service import MediaMetadataProbeService
from src.service.transfers.tag_rules import build_scanned_media_special_tags


class MediaFileScanService:
    DEFAULT_BATCH_SIZE = 100

    def __init__(self, metadata_probe_service: MediaMetadataProbeService | None = None):
        self.metadata_probe_service = metadata_probe_service or MediaMetadataProbeService()

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

    def _scan_single_media(self, media: Media) -> dict[str, bool]:
        file_path = Path(media.path).expanduser().resolve()
        file_exists = file_path.exists() and file_path.is_file()
        updates: dict = {}
        result = {
            "updated": False,
            "invalidated": False,
            "revived": False,
        }

        if media.valid != file_exists:
            # valid 的语义以当前文件状态为准，巡检要把库里状态修正回来。
            updates[Media.valid] = file_exists
            result["invalidated"] = not file_exists
            result["revived"] = file_exists

        if file_exists and media.video_info is None:
            # 只在缺失 video_info 时重新探测，避免每轮全量解析媒体文件。
            metadata = self.metadata_probe_service.probe_file(file_path)
            file_size_bytes = file_path.stat().st_size
            if media.file_size_bytes != file_size_bytes:
                updates[Media.file_size_bytes] = file_size_bytes
            if metadata.resolution and media.resolution != metadata.resolution:
                updates[Media.resolution] = metadata.resolution
            if metadata.duration_seconds > 0 and media.duration_seconds != metadata.duration_seconds:
                updates[Media.duration_seconds] = metadata.duration_seconds
            if metadata.video_info is not None and media.video_info != metadata.video_info:
                updates[Media.video_info] = metadata.video_info

        if file_exists:
            effective_video_info = updates.get(Media.video_info, media.video_info)
            special_tags = build_scanned_media_special_tags(
                media.special_tags,
                video_info=effective_video_info,
                has_subtitle=self._has_sidecar_subtitle(file_path),
            )
            if media.special_tags != special_tags:
                updates[Media.special_tags] = special_tags

        if not updates:
            MovieSubtitleService.sync_movie_subtitles(media.movie)
            return result

        for field, value in updates.items():
            setattr(media, field.name, value)
        media.updated_at = utc_now_for_db()
        media.save(only=[*updates.keys(), Media.updated_at])
        MovieSubtitleService.sync_movie_subtitles(media.movie)
        result["updated"] = True
        return result

    @staticmethod
    def _has_sidecar_subtitle(media_path: Path) -> bool:
        media_directory = media_path.parent
        if not media_directory.exists() or not media_directory.is_dir():
            return False

        media_stem = media_path.stem.lower()
        for subtitle_path in media_directory.iterdir():
            if not subtitle_path.is_file() or subtitle_path.suffix.lower() != ".srt":
                continue
            subtitle_stem = subtitle_path.stem.lower()
            if subtitle_stem == media_stem or subtitle_stem.startswith(f"{media_stem}."):
                return True
        return False

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
