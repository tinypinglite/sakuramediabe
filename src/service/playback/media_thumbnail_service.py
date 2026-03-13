import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loguru import logger

try:
    import av
except ImportError:  # pragma: no cover - exercised by runtime environment, not tests
    av = None

from src.config.config import settings
from src.model import Image, Media, MediaThumbnail, get_database
from src.schema.catalog.actors import ImageResource
from src.schema.playback.media import MediaThumbnailResource


class MediaThumbnailService:
    THUMBNAIL_MAX_RETRIES = 2

    @staticmethod
    def _ensure_worker_database_ready() -> None:
        database = get_database()
        if database.is_closed():
            database.connect()

    @staticmethod
    def _pending_media_ids() -> list[int]:
        query = (
            Media.select(Media.id)
            .where(
                Media.valid == True,
                Media.need_thumbnail_generation == True,
            )
            .order_by(Media.id)
        )
        return [item.id for item in query]

    @staticmethod
    def _image_root_path() -> Path:
        image_root_path = Path(settings.media.import_image_root_path).expanduser()
        if not image_root_path.is_absolute():
            image_root_path = (Path.cwd() / image_root_path).resolve()
        return image_root_path

    @classmethod
    def _thumbnail_directory(cls, media: Media) -> Path:
        return (
            cls._image_root_path()
            / "movies"
            / media.movie.movie_number
            / "media"
            / media.content_fingerprint
            / "thumbnails"
        )

    @staticmethod
    def _lower_process_priority() -> None:
        try:
            os.nice(19)
        except (AttributeError, OSError):
            return

    @staticmethod
    def _parse_offset_seconds(file_path: Path) -> int | None:
        if not file_path.stem.isdigit():
            return None
        offset = int(file_path.stem)
        if offset < 0:
            return None
        return offset

    @classmethod
    def _duration_seconds_for_threshold(cls, media: Media) -> int:
        if media.duration_seconds > 0:
            return media.duration_seconds
        if media.movie.duration_minutes > 0:
            return media.movie.duration_minutes * 60
        return 0

    @staticmethod
    def _expected_thumbnail_count(duration_seconds: int) -> int:
        if duration_seconds <= 0:
            return 0
        return max(1, duration_seconds // 10)

    @staticmethod
    def _minimum_acceptable_thumbnail_count(expected_count: int) -> int:
        if expected_count <= 0:
            return 0
        return max(1, int(expected_count * 0.85))

    @classmethod
    def _collect_parseable_webp_files(cls, webp_dir: Path) -> tuple[list[Path], int]:
        webp_files = list(webp_dir.glob("*.webp"))
        parseable_files: list[tuple[int, Path]] = []
        for webp_file in webp_files:
            offset = cls._parse_offset_seconds(webp_file)
            if offset is not None:
                parseable_files.append((offset, webp_file))
        parseable_files.sort(key=lambda item: (item[0], item[1].name))
        return [item[1] for item in parseable_files], len(webp_files)

    @staticmethod
    def _build_generation_cause(pyav_error: Exception | None) -> str | None:
        if pyav_error is None:
            return None
        return f"pyav={pyav_error}"

    @classmethod
    def _build_insufficient_count_error(
        cls,
        *,
        expected_count: int,
        minimum_count: int,
        actual_count: int,
        pyav_error: Exception | None,
    ) -> str:
        message = (
            f"thumbnail_generation_insufficient_count expected={expected_count} "
            f"minimum={minimum_count} actual={actual_count}"
        )
        cause = cls._build_generation_cause(pyav_error)
        if cause is not None:
            message = f"{message} cause={cause}"
        return message

    @staticmethod
    def _clear_webp_directory(webp_dir: Path) -> None:
        webp_dir.mkdir(parents=True, exist_ok=True)
        for existing_file in webp_dir.glob("*.webp"):
            existing_file.unlink()

    @staticmethod
    def _resolve_generation_duration_seconds(container, stream) -> int:
        stream_duration = getattr(stream, "duration", None)
        stream_time_base = getattr(stream, "time_base", None)
        if stream_duration is not None and stream_time_base:
            duration_seconds = float(stream_duration * stream_time_base)
            if duration_seconds > 0:
                return int(duration_seconds)

        container_duration = getattr(container, "duration", None)
        if container_duration is not None and getattr(av, "time_base", None):
            duration_seconds = float(container_duration / av.time_base)
            if duration_seconds > 0:
                return int(duration_seconds)

        return 0

    @classmethod
    def _generate_webp_with_pyav(
        cls,
        video_path: Path,
        webp_dir: Path,
        *,
        interval_seconds: int = 10,
    ) -> Exception | None:
        if av is None:
            return RuntimeError("pyav_not_installed")

        cls._lower_process_priority()
        container = None
        first_error: Exception | None = None
        try:
            container = av.open(str(video_path))
            if not container.streams.video:
                return RuntimeError("video_stream_missing")

            stream = container.streams.video[0]
            duration_seconds = cls._resolve_generation_duration_seconds(container, stream)
            if duration_seconds <= 0:
                return first_error

            for offset_seconds in range(0, duration_seconds + 1, interval_seconds):
                try:
                    timestamp = int(offset_seconds / float(stream.time_base))
                    container.seek(
                        timestamp,
                        stream=stream,
                        backward=True,
                        any_frame=False,
                    )
                    frame = next(container.decode(stream))
                    image_path = webp_dir / f"{offset_seconds}.webp"
                    frame.to_image().save(image_path, format="WEBP", quality=80)
                except StopIteration:
                    if first_error is None:
                        first_error = RuntimeError(
                            f"decode_frame_missing offset_seconds={offset_seconds}"
                        )
                    logger.warning(
                        "PyAV frame missing media_path={} offset_seconds={}",
                        video_path,
                        offset_seconds,
                    )
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    logger.warning(
                        "PyAV thumbnail generation skipped offset media_path={} offset_seconds={} detail={}",
                        video_path,
                        offset_seconds,
                        exc,
                    )
        except Exception as exc:
            if first_error is None:
                first_error = exc
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception as exc:
                    first_error = exc
        return first_error

    @classmethod
    def _persist_generated_files(cls, media: Media, webp_files: list[Path]) -> int:
        created_count = 0
        image_root = cls._image_root_path()
        with get_database().atomic():
            for webp_file in webp_files:
                offset = cls._parse_offset_seconds(webp_file)
                if offset is None:
                    logger.warning(
                        "Skipping generated thumbnail media_id={} file_name={} reason=offset_parse_failed",
                        media.id,
                        webp_file.name,
                    )
                    continue
                relative_path = webp_file.relative_to(image_root).as_posix()
                image = Image.create(
                    origin=relative_path,
                    small=relative_path,
                    medium=relative_path,
                    large=relative_path,
                )
                MediaThumbnail.create(media=media, image=image, offset=offset)
                created_count += 1
        return created_count

    @staticmethod
    def _mark_success(media: Media) -> None:
        media.need_thumbnail_generation = False
        media.thumbnail_retry_count = 0
        media.thumbnail_last_error = None
        media.save()

    @classmethod
    def _mark_failure(cls, media: Media, error: str, *, terminal: bool = False) -> str:
        media.thumbnail_retry_count += 1
        media.thumbnail_last_error = error
        if terminal or media.thumbnail_retry_count >= cls.THUMBNAIL_MAX_RETRIES:
            media.need_thumbnail_generation = False
            result_key = "terminal_failed_media"
        else:
            media.need_thumbnail_generation = True
            result_key = "retryable_failed_media"
        media.save()
        return result_key

    @staticmethod
    def _failure_type(result_key: str) -> str:
        return "terminal" if result_key == "terminal_failed_media" else "retryable"

    @classmethod
    def _log_aborted(cls, media: Media, reason: str, result_key: str) -> None:
        logger.warning(
            "Generate media thumbnails aborted media_id={} reason={} failure_type={} retry_count={}",
            media.id,
            reason,
            cls._failure_type(result_key),
            media.thumbnail_retry_count,
        )

    @classmethod
    def _process_media(cls, media_id: int) -> dict[str, int]:
        cls._ensure_worker_database_ready()
        media = Media.get_or_none(Media.id == media_id)
        if media is None or not media.valid or not media.need_thumbnail_generation:
            return {}
        if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
            cls._mark_success(media)
            logger.info(
                "Skipping media thumbnail generation media_id={} reason=thumbnails_already_exist",
                media.id,
            )
            return {}
        if not media.content_fingerprint:
            error_key = cls._mark_failure(media, "content_fingerprint_missing", terminal=True)
            cls._log_aborted(media, "content_fingerprint_missing", error_key)
            return {error_key: 1}

        video_path = Path(media.path).expanduser().resolve()
        if not video_path.exists() or not video_path.is_file():
            error_key = cls._mark_failure(media, "video_file_missing")
            cls._log_aborted(media, "video_file_missing", error_key)
            return {error_key: 1}

        logger.info(
            "Generating media thumbnails media_id={} movie_number={} video_path={}",
            media.id,
            media.movie.movie_number,
            video_path,
        )
        started_at = time.time()
        try:
            webp_dir = cls._thumbnail_directory(media)
            cls._clear_webp_directory(webp_dir)
            pyav_error = cls._generate_webp_with_pyav(video_path, webp_dir)

            if pyav_error is not None:
                logger.warning(
                    "PyAV thumbnail generation reported error media_id={} detail={}",
                    media.id,
                    pyav_error,
                )

            parseable_webp_files, total_webp_count = cls._collect_parseable_webp_files(webp_dir)
            parseable_count = len(parseable_webp_files)
            duration_seconds = cls._duration_seconds_for_threshold(media)
            expected_count = cls._expected_thumbnail_count(duration_seconds)
            minimum_count = cls._minimum_acceptable_thumbnail_count(expected_count)

            if expected_count > 0 and parseable_count >= minimum_count:
                generated_count = cls._persist_generated_files(media, parseable_webp_files)
                if generated_count == 0:
                    raise RuntimeError("thumbnail_generation_unparseable_filenames")
                cls._mark_success(media)
                elapsed_ms = int((time.time() - started_at) * 1000)
                if pyav_error is not None:
                    logger.info(
                        "Generated media thumbnails with tolerant success media_id={} generated_thumbnails={} expected_count={} minimum_count={} actual_parseable_count={} total_webp_count={} pyav_error={} elapsed_ms={}",
                        media.id,
                        generated_count,
                        expected_count,
                        minimum_count,
                        parseable_count,
                        total_webp_count,
                        True,
                        elapsed_ms,
                    )
                else:
                    logger.info(
                        "Generated media thumbnails media_id={} generated_thumbnails={} elapsed_ms={}",
                        media.id,
                        generated_count,
                        elapsed_ms,
                    )
                return {"successful_media": 1, "generated_thumbnails": generated_count}

            if expected_count > 0:
                logger.warning(
                    "Generated thumbnail count below threshold media_id={} expected_count={} minimum_count={} actual_parseable_count={} total_webp_count={}",
                    media.id,
                    expected_count,
                    minimum_count,
                    parseable_count,
                    total_webp_count,
                )
                raise RuntimeError(
                    cls._build_insufficient_count_error(
                        expected_count=expected_count,
                        minimum_count=minimum_count,
                        actual_count=parseable_count,
                        pyav_error=pyav_error,
                    )
                )

            if pyav_error is not None:
                raise pyav_error
            if total_webp_count == 0:
                raise RuntimeError("thumbnail_generation_empty")
            if parseable_count == 0:
                raise RuntimeError("thumbnail_generation_unparseable_filenames")

            generated_count = cls._persist_generated_files(media, parseable_webp_files)
            if generated_count == 0:
                raise RuntimeError("thumbnail_generation_unparseable_filenames")

            cls._mark_success(media)
            elapsed_ms = int((time.time() - started_at) * 1000)
            logger.info(
                "Generated media thumbnails media_id={} generated_thumbnails={} elapsed_ms={}",
                media.id,
                generated_count,
                elapsed_ms,
            )
            return {"successful_media": 1, "generated_thumbnails": generated_count}
        except Exception as exc:
            error_key = cls._mark_failure(media, str(exc))
            logger.warning(
                "Generate media thumbnails failed media_id={} detail={} failure_type={} retry_count={}",
                media.id,
                exc,
                cls._failure_type(error_key),
                media.thumbnail_retry_count,
            )
            return {error_key: 1}

    @classmethod
    def generate_pending_thumbnails(cls) -> dict[str, int]:
        media_ids = cls._pending_media_ids()
        started_at = time.time()
        stats = {
            "pending_media": len(media_ids),
            "successful_media": 0,
            "generated_thumbnails": 0,
            "retryable_failed_media": 0,
            "terminal_failed_media": 0,
        }
        if not media_ids:
            logger.info("No pending media for thumbnail generation")
            return stats

        logger.info(
            "Starting media thumbnail generation pending_media={} max_workers={}",
            len(media_ids),
            settings.media.max_thumbnail_process_count,
        )
        with ThreadPoolExecutor(
            max_workers=settings.media.max_thumbnail_process_count,
            thread_name_prefix="media-thumbnail",
        ) as executor:
            futures = [executor.submit(cls._process_media, media_id) for media_id in media_ids]
            for future in as_completed(futures):
                result = future.result()
                for key, value in result.items():
                    stats[key] += value
        elapsed_ms = int((time.time() - started_at) * 1000)
        logger.info(
            "Finished media thumbnail generation pending_media={} successful_media={} generated_thumbnails={} retryable_failed_media={} terminal_failed_media={} elapsed_ms={}",
            stats["pending_media"],
            stats["successful_media"],
            stats["generated_thumbnails"],
            stats["retryable_failed_media"],
            stats["terminal_failed_media"],
            elapsed_ms,
        )
        return stats

    @staticmethod
    def list_media_thumbnails(media_id: int) -> list[MediaThumbnailResource]:
        query = (
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media == media_id)
            .order_by(MediaThumbnail.offset.asc(), MediaThumbnail.id.asc())
        )
        return [
            MediaThumbnailResource(
                thumbnail_id=thumbnail.id,
                media_id=thumbnail.media_id,
                offset_seconds=thumbnail.offset,
                image=ImageResource.from_attributes_model(thumbnail.image),
            )
            for thumbnail in query
        ]
