"""Provider-neutral scan → stage → host write → finalize import pipeline."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import PurePosixPath
from threading import RLock, local
from typing import Any
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel

from src.api.exception.errors import ApiError
from src.common.media_formats import is_supported_video_file_name
from src.common.media_import_status import (
    FAILURE_REASON_IMAGE_DOWNLOAD_FAILED,
    FAILURE_REASON_METADATA_FETCH_FAILED,
    FAILURE_REASON_METADATA_UPSERT_FAILED,
    make_failure_item,
)
from src.common.movie_numbers import (
    parse_movie_number_from_text,
    subtitle_matches_movie_number,
)
from src.common.service_helpers import emit_progress
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Media, MediaLibrary, Movie, VideoItem, get_database
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_REGISTRY,
    ImportFile,
    ImportFileContent,
    ImportPlacement,
    ProviderOperationError,
    ProviderUnavailableError,
    StagedMedia,
)
from src.schema.catalog.subtitles import SubtitleImportStatus
from src.schema.transfers.media_import import ImportResult
from src.service.catalog.catalog_import_service import CatalogImportService
from src.service.catalog.movie_image_service import ImageDownloadError
from src.service.catalog.subtitle_asset_service import SubtitleAssetService
from src.service.playback.provider_helpers import media_handle_for
from src.service.transfers.downloads.common import library_handle_for
from src.service.videos.video_cover_service import VideoCoverService

ImportProgressCallback = Callable[[dict[str, object]], None]
StageReceiptCallback = Callable[[str, dict[str, Any]], None]
StageReceiptCommitCallback = Callable[[str], None]
StageReceiptClearCallback = Callable[[str], None]


class MetadataImportResult(BaseModel):
    movie_number: str
    movie_id: int | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None


class MediaImportService:
    """Import opaque provider refs without reading provider paths in the host."""

    def __init__(
        self,
        provider: Any | None = None,
        image_downloader: Callable[[str, Any], None] | None = None,
        catalog_import_service: CatalogImportService | None = None,
    ):
        self._provider_override = provider
        self.image_downloader = image_downloader
        self._catalog_persist_lock = RLock()
        self.catalog_import_service = catalog_import_service or CatalogImportService(
            image_downloader=image_downloader,
            persist_lock=self._catalog_persist_lock,
        )
        self._worker_local = local()

    def _storage(self, library: MediaLibrary):
        if self._provider_override is not None:
            return self._provider_override
        try:
            return MEDIA_PROVIDER_REGISTRY.storage_for(library_handle_for(library))
        except ProviderUnavailableError as exc:
            raise ApiError(
                503,
                "provider_not_installed",
                "媒体提供方未安装",
                {"provider_key": library.provider_key},
            ) from exc
        except ProviderOperationError as exc:
            raise self._provider_error(exc) from exc

    def _metadata_max_workers(self, total_movies: int) -> int:
        from src.config.config import settings

        return min(max(1, settings.metadata.import_metadata_max_workers), total_movies)

    def _ensure_worker_database_ready(self) -> None:
        database = get_database()
        if database.is_closed():
            database.connect()

    def _import_movie_metadata(self, movie_number: str) -> MetadataImportResult:
        self._ensure_worker_database_ready()
        provider = getattr(self._worker_local, "metadata_provider", None)
        if provider is None:
            from src.metadata.factory import build_javdb_provider

            provider = build_javdb_provider()
            self._worker_local.metadata_provider = provider
        try:
            detail = provider.get_movie_by_number(movie_number)
        except (MetadataNotFoundError, MetadataRequestError, Exception) as exc:
            logger.warning("Import metadata fetch failed movie_number={} detail={}", movie_number, exc)
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason=FAILURE_REASON_METADATA_FETCH_FAILED,
                failure_detail=str(exc),
            )
        try:
            movie, _created = self.catalog_import_service.import_movie_if_missing(
                detail,
                force_subscribed=True,
            )
        except ImageDownloadError as exc:
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason=FAILURE_REASON_IMAGE_DOWNLOAD_FAILED,
                failure_detail=str(exc),
            )
        except Exception as exc:
            logger.exception("Import metadata upsert failed movie_number={}", movie_number)
            return MetadataImportResult(
                movie_number=movie_number,
                failure_reason=FAILURE_REASON_METADATA_UPSERT_FAILED,
                failure_detail=str(exc),
            )
        return MetadataImportResult(movie_number=movie_number, movie_id=movie.id)

    @contextmanager
    def metadata_import_batch(
        self,
        movie_numbers: Sequence[str],
        *,
        thread_name_prefix: str = "import-metadata",
    ):
        if not movie_numbers:
            yield {}
            return
        with ThreadPoolExecutor(
            max_workers=self._metadata_max_workers(len(movie_numbers)),
            thread_name_prefix=thread_name_prefix,
        ) as executor:
            yield {
                number: executor.submit(self._import_movie_metadata, number)
                for number in movie_numbers
            }

    def import_from_source(
        self,
        source_ref: dict[str, Any],
        library_id: int,
        *,
        media_kind: str = "jav",
        source_disposition: str = "keep",
        collection_id: int | None = None,
        progress_callback: ImportProgressCallback | None = None,
        stage_receipt_callback: StageReceiptCallback | None = None,
        stage_receipt_commit_callback: StageReceiptCommitCallback | None = None,
        stage_receipt_clear_callback: StageReceiptClearCallback | None = None,
        operation_namespace: str | None = None,
    ) -> ImportResult:
        if not isinstance(source_ref, dict) or not source_ref:
            raise ApiError(422, "invalid_import_source", "source_ref must be an object")
        if source_disposition not in {"keep", "delete_after_commit"}:
            raise ApiError(422, "invalid_source_disposition", "无效的源处置方式")
        library = MediaLibrary.get_or_none(MediaLibrary.id == library_id)
        if library is None:
            raise ApiError(404, "media_library_not_found", "媒体库不存在")
        if media_kind not in {"jav", "video"}:
            raise ApiError(422, "invalid_media_kind", "无效的媒体类型")
        if media_kind == "jav" and collection_id is not None:
            raise ApiError(422, "invalid_collection", "jav import does not support collection_id")
        storage = self._storage(library)
        try:
            scanned_files = tuple(storage.scan_import_source(source_ref=source_ref))
        except ProviderOperationError as exc:
            raise self._provider_error(exc) from exc
        except Exception as exc:
            logger.exception("Provider import scan failed library_id={}", library_id)
            raise ApiError(502, "provider_scan_failed", "媒体提供方扫描失败") from exc
        for source in scanned_files:
            self._validate_import_file(source)
        from src.config.config import settings

        minimum_video_file_size = settings.media.allowed_min_video_file_size

        failure_items: list[dict[str, str]] = []
        imported_count = skipped_count = failed_count = 0
        created_video_ids: list[int] = []
        new_playable_movies: list[dict[str, object]] = []
        imported_subtitle_paths: set[str] = set()
        finalize_error: Exception | None = None
        import_source_identities: dict[str, str] = {}
        get_import_source_identity = getattr(storage, "get_import_source_identity", None)
        if source_disposition == "keep" and callable(get_import_source_identity):
            for source in scanned_files:
                if not is_supported_video_file_name(source.name):
                    continue
                if media_kind == "jav" and source.size_bytes < minimum_video_file_size:
                    continue
                try:
                    identity = get_import_source_identity(source=source)
                except ProviderOperationError as exc:
                    logger.warning(
                        "Provider import source identity unavailable; falling back to regular import "
                        "library_id={} source={} code={}",
                        library_id,
                        source.name,
                        exc.code,
                    )
                    continue
                except Exception:
                    logger.exception(
                        "Provider import source identity failed; falling back to regular import "
                        "library_id={} source={}",
                        library_id,
                        source.name,
                    )
                    continue
                if identity is None:
                    continue
                if not isinstance(identity, str) or not identity:
                    logger.warning(
                        "Provider returned invalid import source identity; falling back to regular import "
                        "library_id={} source={}",
                        library_id,
                        source.name,
                    )
                    continue
                import_source_identities[source.relative_path] = identity
            if import_source_identities:
                existing_identities = {
                    identity
                    for (identity,) in (
                        Media.select(Media.import_source_identity)
                        .where(
                            (Media.library == library)
                            & Media.import_source_identity.in_(
                                list(import_source_identities.values())
                            )
                        )
                        .tuples()
                    )
                }
                duplicate_paths = {
                    relative_path
                    for relative_path, identity in import_source_identities.items()
                    if identity in existing_identities
                }
                skipped_count += len(duplicate_paths)
                scanned_files = tuple(
                    source
                    for source in scanned_files
                    if source.relative_path not in duplicate_paths
                )
        subtitle_sources = tuple(source for source in scanned_files if self._is_srt(source))
        operation_namespace = operation_namespace or f"import:{uuid4().hex}"
        total = len(scanned_files)
        emit_progress(
            progress_callback,
            event="scan_complete",
            current=0,
            total=total,
            text="媒体提供方扫描完成",
            summary_patch={"imported_count": 0, "skipped_count": 0, "failed_count": 0},
        )

        metadata_numbers = {
            number
            for item in scanned_files
            if media_kind == "jav"
            and is_supported_video_file_name(item.name)
            and item.size_bytes >= minimum_video_file_size
            and (
                number := parse_movie_number_from_text(
                    f"{item.name} {item.relative_path}"
                )
            )
        }
        with self.metadata_import_batch(sorted(metadata_numbers)) as metadata_futures:
            for index, source in enumerate(scanned_files, start=1):
                staged: StagedMedia | None = None
                video: VideoItem | None = None
                media: Media | None = None
                if not is_supported_video_file_name(source.name):
                    if not self._is_srt(source):
                        skipped_count += 1
                    continue
                if media_kind == "jav" and source.size_bytes < minimum_video_file_size:
                    skipped_count += 1
                    continue
                movie_number = parse_movie_number_from_text(f"{source.name} {source.relative_path}")
                if media_kind == "jav" and not movie_number:
                    failed_count += 1
                    failure_items.append(make_failure_item(source.name, "movie_number_not_found"))
                    continue
                if media_kind == "video":
                    movie_number = None
                operation_key = self._operation_key(operation_namespace, index)
                placement = ImportPlacement(
                    relative_path=(
                        f"jav/{movie_number}/{source.name}" if movie_number else f"videos/{source.name}"
                    )
                )
                try:
                    staged = storage.stage_import_file(
                        source=source,
                        placement=placement,
                        source_disposition=source_disposition,
                        operation_key=operation_key,
                    )
                    if not isinstance(staged, StagedMedia):
                        raise ApiError(502, "provider_invalid_response", "媒体提供方返回了无效暂存结果")
                    if stage_receipt_callback is not None:
                        # receipt 必须先落通用任务记录，再尝试写入宿主业务表。
                        stage_receipt_callback(operation_key, staged.receipt)
                    if media_kind == "jav":
                        metadata = metadata_futures[movie_number].result()
                        if metadata.movie_id is None:
                            raise RuntimeError(metadata.failure_detail or metadata.failure_reason or "metadata import failed")
                        movie = Movie.get_by_id(metadata.movie_id)
                        media = self._create_media(
                            storage=storage,
                            movie=movie,
                            video_item=None,
                            library=library,
                            source=source,
                            staged=staged,
                            on_commit=(
                                lambda operation_key=operation_key: stage_receipt_commit_callback(
                                    operation_key
                                )
                                if stage_receipt_commit_callback is not None
                                else None
                            ),
                        )
                        new_playable_movies.append(
                            {"id": movie.id, "movie_number": movie.movie_number, "title": movie.title}
                        )
                    else:
                        with get_database().atomic():
                            video = VideoItem.create(title=self._title_for(source))
                            media = self._create_media(
                                storage=storage,
                                movie=None,
                                video_item=video,
                                library=library,
                                source=source,
                                staged=staged,
                            )
                            if collection_id is not None:
                                from src.service.videos.video_collection_service import (
                                    VideoCollectionService,
                                )

                                VideoCollectionService.add_item(collection_id, video.id)
                            if stage_receipt_commit_callback is not None:
                                stage_receipt_commit_callback(operation_key)
                        created_video_ids.append(video.id)
                except ProviderOperationError as exc:
                    failed_count += 1
                    failure_items.append(make_failure_item(source.name, exc.code))
                    if self._abort_staged(storage, staged) and stage_receipt_clear_callback is not None:
                        stage_receipt_clear_callback(operation_key)
                except Exception as exc:
                    failed_count += 1
                    failure_items.append(make_failure_item(source.name, str(exc)))
                    if self._abort_staged(storage, staged) and stage_receipt_clear_callback is not None:
                        stage_receipt_clear_callback(operation_key)
                else:
                    try:
                        storage.finalize_import(receipt=staged.receipt)
                    except ProviderOperationError as exc:
                        failed_count += 1
                        failure_items.append(make_failure_item(source.name, exc.code))
                        finalize_error = exc
                        logger.exception(
                            "Provider import finalize failed library_id={} source={}",
                            library_id,
                            source.name,
                        )
                    except Exception as exc:
                        failed_count += 1
                        failure_items.append(make_failure_item(source.name, str(exc)))
                        finalize_error = exc
                        logger.exception(
                            "Provider import finalize failed library_id={} source={}",
                            library_id,
                            source.name,
                        )
                    else:
                        import_source_identity = import_source_identities.get(source.relative_path)
                        if import_source_identity is not None and media is not None:
                            media.import_source_identity = import_source_identity
                            media.save(only=[Media.import_source_identity])
                        if stage_receipt_clear_callback is not None:
                            stage_receipt_clear_callback(operation_key)
                        imported_count += 1
                        if video is not None and media is not None:
                            self._generate_video_cover(
                                storage=storage,
                                video=video,
                                media=media,
                            )
                        if media_kind == "jav":
                            failed_count += self._import_sidecar_subtitles(
                                storage=storage,
                                video_source=source,
                                movie_number=movie_number,
                                subtitle_sources=subtitle_sources,
                                imported_subtitle_paths=imported_subtitle_paths,
                                source_disposition=source_disposition,
                                failure_items=failure_items,
                            )
                emit_progress(
                    progress_callback,
                    event="file_finished",
                    current=index,
                    total=total,
                    text=f"已处理 {index}/{total}",
                    summary_patch={
                        "imported_count": imported_count,
                        "skipped_count": skipped_count,
                        "failed_count": failed_count,
                    },
                )
        skipped_count += sum(
            source.relative_path not in imported_subtitle_paths
            for source in subtitle_sources
        )
        if finalize_error is not None:
            raise finalize_error
        return ImportResult(
            imported_count=imported_count,
            skipped_count=skipped_count,
            failed_count=failed_count,
            new_playable_movies=new_playable_movies,
            created_video_ids=created_video_ids,
        )

    @staticmethod
    def _create_media(
        *,
        storage,
        movie,
        video_item,
        library,
        source: ImportFile,
        staged: StagedMedia,
        on_commit: Callable[[], None] | None = None,
    ) -> Media:
        with get_database().atomic():
            media = Media.create(
                movie=movie,
                video_item=video_item,
                library=library,
                storage_ref=staged.storage_ref,
                file_name=source.name,
                file_size_bytes=staged.size_bytes,
                duration_seconds=max(0, staged.duration_seconds or 0),
                video_info=staged.video_info,
                valid=True,
            )
            file_hash = storage.compute_file_hash(media=media_handle_for(media))
            if not isinstance(file_hash, str) or not file_hash:
                raise ValueError("provider returned an invalid media file hash")
            media.file_hash = file_hash
            media.save(only=[Media.file_hash])
            if on_commit is not None:
                on_commit()
            return media

    @staticmethod
    def _generate_video_cover(*, storage: Any, video: VideoItem, media: Media) -> None:
        open_cover_source = getattr(storage, "open_cover_source", None)
        if not callable(open_cover_source):
            logger.warning(
                "Video cover skipped because provider does not support cover source video_id={}",
                video.id,
            )
            return
        try:
            with open_cover_source(media=media_handle_for(media)) as source:
                VideoCoverService.generate_cover(video, source)
        except Exception as exc:
            logger.warning("Video cover skipped video_id={} detail={}", video.id, exc)

    @staticmethod
    def _title_for(source: ImportFile) -> str:
        name = source.name
        return name.rsplit(".", 1)[0] if "." in name else name

    @staticmethod
    def _is_srt(source: ImportFile) -> bool:
        return source.name.lower().endswith(".srt")

    @classmethod
    def _import_sidecar_subtitles(
        cls,
        *,
        storage,
        video_source: ImportFile,
        movie_number: str,
        subtitle_sources: tuple[ImportFile, ...],
        imported_subtitle_paths: set[str],
        source_disposition: str,
        failure_items: list[dict[str, str]],
    ) -> int:
        video_parent = PurePosixPath(video_source.relative_path).parent
        failed_count = 0
        for subtitle_source in subtitle_sources:
            if subtitle_source.relative_path in imported_subtitle_paths:
                continue
            if PurePosixPath(subtitle_source.relative_path).parent != video_parent:
                continue
            if not subtitle_matches_movie_number(subtitle_source.name, movie_number):
                continue
            imported_subtitle_paths.add(subtitle_source.relative_path)
            try:
                imported_file = storage.read_import_file(source=subtitle_source)
                if (
                    not isinstance(imported_file, ImportFileContent)
                    or not isinstance(imported_file.content, bytes)
                    or not isinstance(imported_file.deletion_receipt, dict)
                    or not imported_file.deletion_receipt
                ):
                    raise ValueError("provider returned invalid subtitle content")
                result = SubtitleAssetService.import_subtitle_content(
                    movie_number,
                    imported_file.content,
                    subtitle_source.name,
                )
                if result.status not in {
                    SubtitleImportStatus.IMPORTED,
                    SubtitleImportStatus.DUPLICATE,
                }:
                    raise ValueError(f"subtitle import rejected: {result.status}")
                if source_disposition == "delete_after_commit":
                    storage.delete_import_file(receipt=imported_file.deletion_receipt)
            except ProviderOperationError as exc:
                logger.warning(
                    "Import sidecar subtitle failed movie_number={} source={} code={}",
                    movie_number,
                    subtitle_source.name,
                    exc.code,
                )
                failure_items.append(make_failure_item(subtitle_source.name, exc.code))
                failed_count += 1
            except Exception as exc:
                logger.warning(
                    "Import sidecar subtitle failed movie_number={} source={} detail={}",
                    movie_number,
                    subtitle_source.name,
                    exc,
                )
                failure_items.append(make_failure_item(subtitle_source.name, str(exc)))
                failed_count += 1
        return failed_count

    @staticmethod
    def _validate_import_file(source: object) -> None:
        if not isinstance(source, ImportFile):
            raise ApiError(502, "provider_invalid_response", "媒体提供方返回了无效文件")
        name = source.name
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise ApiError(502, "provider_invalid_response", "媒体提供方返回了不安全文件名")
        if (
            not isinstance(source.size_bytes, int)
            or isinstance(source.size_bytes, bool)
            or source.size_bytes < 0
            or not isinstance(source.is_video, bool)
        ):
            raise ApiError(
                502, "provider_invalid_response", "媒体提供方返回了无效文件信息"
            )

    @staticmethod
    def _operation_key(operation_namespace: str, index: int) -> str:
        return f"{operation_namespace}:{index}"

    @staticmethod
    def _abort_staged(storage, staged: object) -> bool:
        if not isinstance(staged, StagedMedia):
            return True
        try:
            storage.abort_import(receipt=staged.receipt)
            return True
        except Exception:
            logger.exception("Provider import abort failed")
            return False

    @staticmethod
    def _provider_error(exc: ProviderOperationError) -> ApiError:
        status = {
            "invalid_config": 422,
            "authentication_failed": 401,
            "source_not_found": 404,
            "unsupported": 422,
            "unavailable": 503,
        }.get(exc.code, 502)
        return ApiError(
            status,
            f"provider_{exc.code}",
            exc.safe_message,
            {"provider_key": exc.provider_key, "operation": exc.operation},
        )
