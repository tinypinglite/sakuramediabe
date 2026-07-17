import asyncio
import io
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from loguru import logger
from peewee import fn

try:
    import av
except ImportError:  # pragma: no cover - exercised by runtime environment, not tests
    av = None

from src.config.config import settings
from src.lib.cloud115 import (
    Cloud115AuthError,
    Cloud115CipherError,
    Cloud115HlsSegmentReader,
    Cloud115MembershipRequiredError,
    Cloud115RateLimitedError,
    Cloud115RequestError,
    Cloud115VideoNotReadyError,
    VideoDefinition,
    VideoSegment,
)
from src.model import (
    Image,
    Media,
    MediaLibrary,
    MediaThumbnail,
    ResourceTaskState,
    get_database,
)
from src.schema.catalog.actors import ImageResource
from src.schema.playback.media import MediaThumbnailResource
from src.service.system.activity_service import ActivityService
from src.service.system.resource_task_state_service import ResourceTaskStateService


def _parse_resolution(resolution: str | None) -> tuple[int | None, int | None]:
    # 媒体分辨率为固定 "宽x高" 字符串（探测时由 stream 宽高规范生成），解析成整数对供缩略图复用。
    if not resolution:
        return None, None
    parts = resolution.lower().split("x")
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None, None


class _Cloud115HlsDeferredError(RuntimeError):
    """HLS 尚未形成可抽帧时间轴；保持任务 pending 等待下轮。"""


class MediaThumbnailService:
    TASK_KEY = "media_thumbnail_generation"
    THUMBNAIL_MAX_RETRIES = 2
    INTERRUPTED_GENERATION_ERROR_MESSAGE = "媒体缩略图生成任务中断，等待重试"
    # 115 会把 UA 绑定进 HLS variant/TS URL，签发与消费必须逐字节一致。
    CLOUD115_THUMBNAIL_UA = "Mozilla/5.0 SakuraMedia-Thumbnail/1.0"
    CLOUD115_HLS_SEGMENT_MAX_WORKERS = 3
    THUMBNAIL_INTERVAL_SECONDS = 10
    # 账号或上游级故障与具体媒体无关，延后该媒体且不消耗它的有限重试次数。
    CLOUD115_SYSTEM_FAILURES = (
        Cloud115AuthError,
        Cloud115CipherError,
        Cloud115MembershipRequiredError,
        Cloud115RateLimitedError,
        Cloud115RequestError,
        Cloud115VideoNotReadyError,
    )

    @staticmethod
    def _ensure_worker_database_ready() -> None:
        database = get_database()
        if database.is_closed():
            database.connect()

    @staticmethod
    def _is_cloud115_media(media: Media) -> bool:
        # backend 判定的权威来源是所属库（Media 不冗余 backend 字段）。
        from src.model.enums import MediaLibraryBackend

        library = media.library
        return library is not None and library.backend == MediaLibraryBackend.CLOUD115.value

    @classmethod
    def _select_lowest_hls_definition(
        cls,
        definitions: list[VideoDefinition],
    ) -> VideoDefinition:
        if not definitions:
            raise _Cloud115HlsDeferredError("cloud115_hls_definitions_empty")

        def _sort_key(definition: VideoDefinition) -> tuple[int, int, int, int]:
            width, height = _parse_resolution(definition.resolution)
            if width is None or height is None or width <= 0 or height <= 0:
                return (1, 0, 0, max(0, definition.bandwidth))
            return (0, width * height, height, max(0, definition.bandwidth))

        # 优先按真实像素面积选最低清晰度；全部缺失尺寸时按带宽最低者确定。
        parseable = [
            definition
            for definition in definitions
            if all(
                value is not None and value > 0
                for value in _parse_resolution(definition.resolution)
            )
        ]
        candidates = parseable or definitions
        return min(candidates, key=_sort_key)

    @classmethod
    def _build_hls_thumbnail_targets(
        cls,
        segments: list[VideoSegment],
        *,
        interval_seconds: int | None = None,
    ) -> tuple[list[tuple[VideoSegment, list[int]]], int]:
        interval = interval_seconds or cls.THUMBNAIL_INTERVAL_SECONDS
        if interval <= 0:
            raise ValueError("interval_seconds must be positive")

        timeline: list[tuple[VideoSegment, float, float]] = []
        cursor = 0.0
        for segment in segments:
            duration = max(0.0, float(segment.duration_seconds))
            if duration <= 0:
                continue
            end = cursor + duration
            timeline.append((segment, cursor, end))
            cursor = end
        if not timeline or cursor <= 0:
            raise _Cloud115HlsDeferredError("cloud115_hls_segments_empty")

        grouped: dict[int, tuple[VideoSegment, list[int]]] = {}
        timeline_index = 0
        target = 0
        while target < cursor:
            # 半开区间 [start, end)：精确落在边界的目标归下一个分片。
            while (
                timeline_index < len(timeline) - 1
                and target >= timeline[timeline_index][2]
            ):
                timeline_index += 1
            segment = timeline[timeline_index][0]
            item = grouped.setdefault(segment.index, (segment, []))
            item[1].append(target)
            target += interval
        return list(grouped.values()), sum(len(offsets) for _, offsets in grouped.values())

    @classmethod
    async def _resolve_cloud115_hls_targets(
        cls,
        media: Media,
    ) -> tuple[list[tuple[VideoSegment, list[int]]], int, str]:
        from src.service.playback.cloud115_backend_service import cloud115_client_for

        locator = media.backend_locator or {}
        pickcode = locator.get("pickcode")
        if not pickcode:
            raise RuntimeError("cloud115_locator_missing")

        async with cloud115_client_for(
            media.library,
            user_agent=cls.CLOUD115_THUMBNAIL_UA,
        ) as client:
            info = await client.get_video_info(pickcode)
            definition = cls._select_lowest_hls_definition(info.definitions)
            segments = await client.get_video_segments_for_definition(definition)

        targets, expected_count = cls._build_hls_thumbnail_targets(segments)
        definition_label = (
            definition.label or definition.resolution or str(definition.bandwidth)
        )
        source_label = f"cloud115-hls:{pickcode}:{definition_label}"
        return targets, expected_count, source_label

    @classmethod
    def _decode_hls_segment_to_webp(
        cls,
        segment: VideoSegment,
        offsets: list[int],
        webp_dir: Path,
    ) -> int:
        if av is None:
            raise RuntimeError("pyav_not_installed")

        reader = Cloud115HlsSegmentReader(
            segment.url,
            user_agent=cls.CLOUD115_THUMBNAIL_UA,
        )
        container = None
        try:
            # 显式指定 mpegts，避免探测阶段为了识别格式读取过多分片内容。
            container = av.open(reader, format="mpegts")
            if not container.streams.video:
                raise RuntimeError(f"hls_video_stream_missing segment={segment.index}")
            stream = container.streams.video[0]
            clean_frame = next(
                (frame for frame in container.decode(stream) if not frame.is_corrupt),
                None,
            )
            if clean_frame is None:
                raise RuntimeError(f"hls_clean_frame_missing segment={segment.index}")

            # 同一 TS 可能覆盖两个固定 10 秒目标；只编码一次，再写入多个 offset 文件。
            buffer = io.BytesIO()
            clean_frame.to_image().save(buffer, format="WEBP", quality=80)
            content = buffer.getvalue()
            for offset in offsets:
                (webp_dir / f"{offset}.webp").write_bytes(content)
            logger.debug(
                "Decoded cloud115 HLS segment segment_index={} offsets={} fetched_bytes={}",
                segment.index,
                offsets,
                reader.fetched_bytes,
            )
            return len(offsets)
        finally:
            if container is not None:
                container.close()
            reader.close()

    @classmethod
    def _generate_cloud115_hls_webp(
        cls,
        targets: list[tuple[VideoSegment, list[int]]],
        webp_dir: Path,
        *,
        source_label: str,
    ) -> Exception | None:
        first_error: Exception | None = None
        with ThreadPoolExecutor(
            max_workers=cls.CLOUD115_HLS_SEGMENT_MAX_WORKERS,
            thread_name_prefix="cloud115-hls-thumbnail",
        ) as executor:
            futures = {
                executor.submit(
                    cls._decode_hls_segment_to_webp,
                    segment,
                    offsets,
                    webp_dir,
                ): (segment, offsets)
                for segment, offsets in targets
            }
            for future in as_completed(futures):
                segment, offsets = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    logger.warning(
                        "Cloud115 HLS thumbnail segment failed source={} "
                        "segment_index={} offsets={} detail={}",
                        source_label,
                        segment.index,
                        offsets,
                        exc,
                    )
        return first_error

    @staticmethod
    def _pending_media_ids() -> list[int]:
        matching_state_query = ResourceTaskState.select(ResourceTaskState.id).where(
            ResourceTaskState.task_key == MediaThumbnailService.TASK_KEY,
            ResourceTaskState.resource_type == "media",
            ResourceTaskState.resource_id == Media.id,
        )
        query = (
            Media.select(Media.id)
            .where(
                Media.valid == True,
                (
                    ~fn.EXISTS(matching_state_query)
                    | fn.EXISTS(
                        matching_state_query.where(
                            ResourceTaskState.state == ResourceTaskStateService.STATE_PENDING
                        )
                    )
                    | fn.EXISTS(
                        matching_state_query.where(
                            ResourceTaskState.state == ResourceTaskStateService.STATE_FAILED,
                            ResourceTaskStateService.build_retryable_extra_condition(ResourceTaskState.extra),
                        )
                    )
                ),
            )
            .order_by(Media.id)
        )
        return [item.id for item in query]

    @staticmethod
    def _cloud115_media_ids(media_ids: list[int]) -> set[int]:
        if not media_ids:
            return set()

        from src.model.enums import MediaLibraryBackend

        query = (
            Media.select(Media.id)
            .join(MediaLibrary)
            .where(
                Media.id.in_(media_ids),
                MediaLibrary.backend == MediaLibraryBackend.CLOUD115.value,
            )
        )
        return {media.id for media in query}

    @classmethod
    def count_pending_media(cls) -> int:
        # 待生成缩略图的媒体文件数量：直接复用 _pending_media_ids 的判定口径，确保与缩略图生成实际处理范围完全一致。
        return len(cls._pending_media_ids())

    @staticmethod
    def _image_root_path() -> Path:
        image_root_path = Path(settings.media.import_image_root_path).expanduser()
        if not image_root_path.is_absolute():
            image_root_path = (Path.cwd() / image_root_path).resolve()
        return image_root_path

    @classmethod
    def _read_thumbnail_dimensions(
        cls,
        image_origin: str,
    ) -> tuple[int | None, int | None]:
        from PIL import Image as PILImage

        image_path = cls._image_root_path() / image_origin
        with PILImage.open(image_path) as image:
            return image.size

    @classmethod
    def _thumbnail_directory(cls, media: Media) -> Path:
        # 按归属分目录：JAV 用 movies/番号，非 JAV 用 videos/video_item_id，后缀沿用指纹。
        if media.movie_number:
            namespace = Path("movies") / media.movie_number
        else:
            namespace = Path("videos") / str(media.video_item_id)
        return (
            cls._image_root_path()
            / namespace
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
        # 仅 JAV 媒体可回退到影片时长；非 JAV 无此元数据，依赖探测写入的 duration_seconds。
        if media.movie_number and media.movie.duration_minutes > 0:
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
        video_source,
        webp_dir: Path,
        *,
        interval_seconds: int = 10,
        source_label: str | None = None,
    ) -> Exception | None:
        """从本地视频每 interval_seconds 抽一帧存入 webp_dir。"""
        if av is None:
            return RuntimeError("pyav_not_installed")

        video_path = source_label or str(video_source)
        cls._lower_process_priority()
        container = None
        first_error: Exception | None = None
        try:
            container = av.open(video_source)
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
        # 非 JAV 媒体的缩略图不进图像检索向量库，直接落 SKIPPED 终态；JAV 维持 PENDING 待索引。
        initial_index_status = (
            MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING
            if media.movie_number
            else MediaThumbnail.JOYTAG_INDEX_STATUS_SKIPPED
        )
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
                MediaThumbnail.create(
                    media=media,
                    image=image,
                    offset=offset,
                    joytag_index_status=initial_index_status,
                )
                created_count += 1
        return created_count

    @staticmethod
    def _mark_success(media: Media) -> None:
        ResourceTaskStateService.mark_succeeded(
            MediaThumbnailService.TASK_KEY,
            media.id,
            extra_patch={"terminal": False},
        )

    @classmethod
    def _mark_failure(cls, media: Media, error: str, *, terminal: bool = False) -> str:
        task_state = ResourceTaskStateService.get_state_or_default(cls.TASK_KEY, media.id)
        is_terminal = bool(terminal or task_state.attempt_count >= cls.THUMBNAIL_MAX_RETRIES)
        ResourceTaskStateService.mark_failed(
            cls.TASK_KEY,
            media.id,
            error,
            extra_patch={"terminal": is_terminal},
        )
        result_key = "terminal_failed_media" if is_terminal else "retryable_failed_media"
        return result_key

    @staticmethod
    def _failure_type(result_key: str) -> str:
        return "terminal" if result_key == "terminal_failed_media" else "retryable"

    @classmethod
    def _log_aborted(cls, media: Media, reason: str, result_key: str) -> None:
        task_state = ResourceTaskStateService.get_state_or_default(cls.TASK_KEY, media.id)
        logger.warning(
            "Generate media thumbnails aborted media_id={} reason={} failure_type={} retry_count={}",
            media.id,
            reason,
            cls._failure_type(result_key),
            task_state.attempt_count,
        )

    @classmethod
    def recover_interrupted_running_media(cls, *, error_message: str | None = None) -> int:
        normalized_error = (error_message or "").strip() or cls.INTERRUPTED_GENERATION_ERROR_MESSAGE
        return ResourceTaskStateService.recover_running_records(cls.TASK_KEY, normalized_error)

    @classmethod
    def _process_media(cls, media_id: int) -> dict[str, int]:
        cls._ensure_worker_database_ready()
        media = Media.get_or_none(Media.id == media_id)
        if media is None or not media.valid:
            return {}
        if MediaThumbnail.select().where(MediaThumbnail.media == media).exists():
            cls._mark_success(media)
            logger.info(
                "Skipping media thumbnail generation media_id={} reason=thumbnails_already_exist",
                media.id,
            )
            return {}
        if not media.content_fingerprint:
            ResourceTaskStateService.mark_started(cls.TASK_KEY, media.id)
            error_key = cls._mark_failure(media, "content_fingerprint_missing", terminal=True)
            cls._log_aborted(media, "content_fingerprint_missing", error_key)
            return {error_key: 1}

        # Cloud115 只走官方 HLS；转码未就绪时保持 pending，不回退整文件直链。
        is_cloud115 = cls._is_cloud115_media(media)
        hls_targets: list[tuple[VideoSegment, list[int]]] = []
        hls_expected_count = 0
        if is_cloud115:
            try:
                hls_targets, hls_expected_count, source_label = asyncio.run(
                    cls._resolve_cloud115_hls_targets(media)
                )
            except (cls.CLOUD115_SYSTEM_FAILURES, _Cloud115HlsDeferredError) as exc:
                # HLS 尚不可用时不进入 running，保留 attempt_count 等待后续调度轮次。
                logger.warning(
                    "Deferred cloud115 HLS thumbnail generation media_id={} detail={} retry_count={}",
                    media.id,
                    exc,
                    ResourceTaskStateService.get_state_or_default(
                        cls.TASK_KEY, media.id
                    ).attempt_count,
                )
                return {"deferred_media": 1}
            except Exception as exc:
                ResourceTaskStateService.mark_started(cls.TASK_KEY, media.id)
                error_key = cls._mark_failure(
                    media,
                    f"cloud115_hls_prepare_failed: {exc}",
                )
                cls._log_aborted(media, "cloud115_hls_prepare_failed", error_key)
                return {error_key: 1}
            ResourceTaskStateService.mark_started(cls.TASK_KEY, media.id)
        else:
            ResourceTaskStateService.mark_started(cls.TASK_KEY, media.id)
            video_path = Path(media.path).expanduser().resolve()
            if not video_path.exists() or not video_path.is_file():
                error_key = cls._mark_failure(media, "video_file_missing")
                cls._log_aborted(media, "video_file_missing", error_key)
                return {error_key: 1}
            source_label = str(video_path)

        logger.info(
            "Generating media thumbnails media_id={} movie_number={} video_path={}",
            media.id,
            # 解耦后非 JAV 媒体 movie 为空，读外键原始列（None-safe），避免解引用 None 崩溃整轮任务。
            media.movie_number,
            source_label,
        )
        started_at = time.time()
        try:
            webp_dir = cls._thumbnail_directory(media)
            cls._clear_webp_directory(webp_dir)
            if is_cloud115:
                pyav_error = cls._generate_cloud115_hls_webp(
                    hls_targets,
                    webp_dir,
                    source_label=source_label,
                )
            else:
                pyav_error = cls._generate_webp_with_pyav(str(video_path), webp_dir)

            if pyav_error is not None:
                logger.warning(
                    "PyAV thumbnail generation reported error media_id={} detail={}",
                    media.id,
                    pyav_error,
                )

            parseable_webp_files, total_webp_count = cls._collect_parseable_webp_files(webp_dir)
            parseable_count = len(parseable_webp_files)
            if is_cloud115:
                expected_count = hls_expected_count
            else:
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

            if expected_count > 0 and pyav_error is not None:
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
            task_state = ResourceTaskStateService.get_state_or_default(cls.TASK_KEY, media.id)
            logger.warning(
                "Generate media thumbnails failed media_id={} detail={} failure_type={} retry_count={}",
                media.id,
                exc,
                cls._failure_type(error_key),
                task_state.attempt_count,
            )
            return {error_key: 1}

    @staticmethod
    def _emit_progress(progress_callback, **payload) -> None:
        if progress_callback is None:
            return
        progress_callback(payload)

    @classmethod
    def generate_pending_thumbnails(cls, progress_callback=None) -> dict[str, int]:
        media_ids = cls._pending_media_ids()
        started_at = time.time()
        stats = {
            "pending_media": len(media_ids),
            "successful_media": 0,
            "generated_thumbnails": 0,
            "deferred_media": 0,
            "retryable_failed_media": 0,
            "terminal_failed_media": 0,
        }
        if not media_ids:
            logger.info("No pending media for thumbnail generation")
            return stats

        cls._emit_progress(
            progress_callback,
            current=0,
            total=len(media_ids),
            text="开始生成媒体缩略图",
            summary_patch=stats,
        )
        logger.info(
            "Starting media thumbnail generation pending_media={} "
            "local_media_workers={} cloud115_media_workers=1 hls_segment_workers={}",
            len(media_ids),
            settings.media.max_thumbnail_process_count,
            cls.CLOUD115_HLS_SEGMENT_MAX_WORKERS,
        )

        cloud115_media_ids = cls._cloud115_media_ids(media_ids)
        local_media_ids = [
            media_id for media_id in media_ids if media_id not in cloud115_media_ids
        ]
        ordered_cloud115_media_ids = [
            media_id for media_id in media_ids if media_id in cloud115_media_ids
        ]
        completed_count = 0

        def record_result(result: dict[str, int]) -> None:
            nonlocal completed_count
            completed_count += 1
            for key, value in result.items():
                stats[key] += value
            cls._emit_progress(
                progress_callback,
                current=completed_count,
                total=len(media_ids),
                text=f"已处理缩略图任务 {completed_count}/{len(media_ids)}",
                summary_patch=stats,
            )

        # 本地抽帧保留受控并行；Cloud115 媒体严格串行，单媒体内部最多并发 3 个 TS。
        with ThreadPoolExecutor(
            max_workers=settings.media.max_thumbnail_process_count,
            thread_name_prefix="media-thumbnail-local",
        ) as executor:
            local_futures = [
                executor.submit(
                    ActivityService.wrap_current_task_run_context(cls._process_media),
                    media_id,
                )
                for media_id in local_media_ids
            ]
            for media_id in ordered_cloud115_media_ids:
                record_result(cls._process_media(media_id))
            for future in as_completed(local_futures):
                record_result(future.result())
        elapsed_ms = int((time.time() - started_at) * 1000)
        logger.info(
            "Finished media thumbnail generation pending_media={} successful_media={} generated_thumbnails={} deferred_media={} retryable_failed_media={} terminal_failed_media={} elapsed_ms={}",
            stats["pending_media"],
            stats["successful_media"],
            stats["generated_thumbnails"],
            stats["deferred_media"],
            stats["retryable_failed_media"],
            stats["terminal_failed_media"],
            elapsed_ms,
        )
        return stats

    @staticmethod
    def list_media_thumbnails(media_id: int) -> list[MediaThumbnailResource]:
        thumbnails = list(
            MediaThumbnail.select(MediaThumbnail, Image)
            .join(Image)
            .where(MediaThumbnail.media == media_id)
            .order_by(MediaThumbnail.offset.asc(), MediaThumbnail.id.asc())
        )
        width, height = None, None
        if thumbnails:
            try:
                width, height = MediaThumbnailService._read_thumbnail_dimensions(
                    thumbnails[0].image.origin
                )
            except Exception as exc:
                logger.warning(
                    "Resolve media thumbnail dimensions failed media_id={} detail={}",
                    media_id,
                    exc,
                )
        return [
            MediaThumbnailResource(
                thumbnail_id=thumbnail.id,
                media_id=thumbnail.media_id,
                offset_seconds=thumbnail.offset,
                image=ImageResource.from_attributes_model(thumbnail.image),
                width=width,
                height=height,
            )
            for thumbnail in thumbnails
        ]
