import os
import time

from peewee import fn

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError, MetadataRequestError
from src.model import Actor, Media, MediaLibrary, MediaThumbnail, Movie
from src.schema.system.status import (
    StatusActorSummary,
    StatusEmbeddingServiceSummary,
    StatusImageSearchIndexingSummary,
    StatusImageSearchResource,
    StatusImageSearchVectorStoreSummary,
    StatusMediaFileSummary,
    StatusMediaLibrarySummary,
    StatusMetadataProviderTestError,
    StatusMetadataProviderTestResource,
    StatusMovieSummary,
    StatusResource,
    StatusThumbnailSummary,
)
from src.service.discovery.embedding_client import (
    EmbeddingClientError,
    get_embedding_client,
)
from src.service.discovery.qdrant_thumbnail_store import (
    QdrantThumbnailStore,
    get_qdrant_thumbnail_store,
)
from src.service.playback.media_thumbnail_service import MediaThumbnailService


class StatusService:
    FEMALE_GENDER = 1
    BACKEND_VERSION_ENV_KEY = "SAKURAMEDIA_BACKEND_VERSION"
    BACKEND_VERSION_DEFAULT = "dev-local"
    METADATA_PROVIDER_TEST_MOVIE_NUMBER = "SSNI-888"

    @classmethod
    def get_status(cls) -> StatusResource:
        female_total = Actor.select().where(Actor.gender == cls.FEMALE_GENDER).count()
        female_subscribed = (
            Actor.select()
            .where((Actor.gender == cls.FEMALE_GENDER) & (Actor.is_subscribed == True))
            .count()
        )

        movie_total = Movie.select().count()
        movie_subscribed = Movie.select().where(Movie.is_subscribed == True).count()
        movie_playable = (
            Media.select(fn.COUNT(fn.DISTINCT(Media.movie)))
            .where(Media.valid == True)
            .scalar()
            or 0
        )

        media_file_total = Media.select().count()
        media_file_total_size_bytes = (
            Media.select(fn.COALESCE(fn.SUM(Media.file_size_bytes), 0)).scalar() or 0
        )

        media_library_total = MediaLibrary.select().count()

        # 待生成缩略图的媒体文件数复用缩略图服务的判定口径；缩略图文件数即 MediaThumbnail 行数（与 Media 一对多）。
        pending_thumbnail_media = MediaThumbnailService.count_pending_media()
        retry_wait_thumbnail_media = MediaThumbnailService.count_retry_wait_media()
        terminal_thumbnail_media = MediaThumbnailService.count_terminal_failed_media()
        thumbnail_total = MediaThumbnail.select().count()

        return StatusResource(
            backend_version=cls._resolve_backend_version(),
            actors=StatusActorSummary(
                female_total=int(female_total),
                female_subscribed=int(female_subscribed),
            ),
            movies=StatusMovieSummary(
                total=int(movie_total),
                subscribed=int(movie_subscribed),
                playable=int(movie_playable),
            ),
            media_files=StatusMediaFileSummary(
                total=int(media_file_total),
                total_size_bytes=int(media_file_total_size_bytes),
            ),
            media_libraries=StatusMediaLibrarySummary(total=int(media_library_total)),
            thumbnails=StatusThumbnailSummary(
                pending_media=int(pending_thumbnail_media),
                retry_wait_media=int(retry_wait_thumbnail_media),
                terminal_failed_media=int(terminal_thumbnail_media),
                total=int(thumbnail_total),
            ),
        )

    @classmethod
    def _resolve_backend_version(cls) -> str:
        # 后端版本由镜像构建阶段注入，未注入时回退本地开发默认值。
        backend_version = os.getenv(cls.BACKEND_VERSION_ENV_KEY)
        if backend_version:
            return backend_version
        return cls.BACKEND_VERSION_DEFAULT

    @classmethod
    def get_image_search_status(cls) -> StatusImageSearchResource:
        embedding_service = cls._probe_embedding_service()
        image_search_vector_store = cls._probe_image_search_vector_store()
        indexing = cls._indexing_status()
        return StatusImageSearchResource(
            healthy=bool(embedding_service.healthy and image_search_vector_store.healthy),
            checked_at=utc_now_for_db(),
            embedding_service=embedding_service,
            image_search_vector_store=image_search_vector_store,
            indexing=indexing,
        )

    @classmethod
    def test_metadata_provider(cls, provider: str) -> StatusMetadataProviderTestResource:
        normalized_provider = provider.strip().lower()
        start_at = time.time()
        try:
            if normalized_provider == "javdb":
                return cls._test_javdb_provider(start_at=start_at)
            raise ValueError(f"unsupported metadata provider: {provider}")
        except MetadataNotFoundError as exc:
            return cls._build_metadata_provider_failure(
                provider=normalized_provider,
                start_at=start_at,
                error=StatusMetadataProviderTestError(
                    type="metadata_not_found",
                    message=str(exc),
                    resource=exc.resource,
                    lookup_value=exc.lookup_value,
                ),
            )
        except MetadataRequestError as exc:
            return cls._build_metadata_provider_failure(
                provider=normalized_provider,
                start_at=start_at,
                error=StatusMetadataProviderTestError(
                    type="metadata_request_error",
                    message=str(exc),
                    method=exc.method,
                    url=exc.url,
                ),
            )
        except Exception as exc:
            return cls._build_metadata_provider_failure(
                provider=normalized_provider,
                start_at=start_at,
                error=StatusMetadataProviderTestError(
                    type="unexpected_error",
                    message=str(exc),
                ),
            )

    @classmethod
    def _test_javdb_provider(cls, *, start_at: float) -> StatusMetadataProviderTestResource:
        # JavDB 联通性以真实按番号搜索并拉取详情为准；JavDB 请求永远直连（不叠 metadata proxy）。
        detail = build_javdb_provider().get_movie_by_number(
            cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER
        )
        return StatusMetadataProviderTestResource(
            healthy=True,
            checked_at=utc_now_for_db(),
            provider="javdb",
            movie_number=cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER,
            elapsed_ms=cls._elapsed_ms(start_at),
            javdb_id=detail.javdb_id,
            title=detail.title,
            actors_count=len(detail.actors),
            tags_count=len(detail.tags),
        )

    @classmethod
    def _build_metadata_provider_failure(
        cls,
        *,
        provider: str,
        start_at: float,
        error: StatusMetadataProviderTestError,
    ) -> StatusMetadataProviderTestResource:
        return StatusMetadataProviderTestResource(
            healthy=False,
            checked_at=utc_now_for_db(),
            provider=provider,
            movie_number=cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER,
            elapsed_ms=cls._elapsed_ms(start_at),
            error=error,
        )

    @staticmethod
    def _elapsed_ms(start_at: float) -> int:
        return int((time.time() - start_at) * 1000)

    @classmethod
    def _probe_embedding_service(cls) -> StatusEmbeddingServiceSummary:
        try:
            space = get_embedding_client().describe()
        except EmbeddingClientError as exc:
            return StatusEmbeddingServiceSummary(
                healthy=False,
                endpoint=str(settings.image_search.inference_base_url),
                error=exc.message,
            )
        except Exception as exc:
            return StatusEmbeddingServiceSummary(
                healthy=False,
                endpoint=str(settings.image_search.inference_base_url),
                error=str(exc),
            )
        return StatusEmbeddingServiceSummary(
            healthy=True,
            endpoint=str(settings.image_search.inference_base_url),
            space_id=space.space_id,
            dimension=space.dimension,
            modalities=sorted(space.modalities),
        )

    @staticmethod
    def _probe_image_search_vector_store() -> StatusImageSearchVectorStoreSummary:
        try:
            store = get_qdrant_thumbnail_store()
            status = store.inspect_status()
            return StatusImageSearchVectorStoreSummary(
                healthy=bool(status.get("healthy", False)),
                url=str(status.get("url", getattr(store, "url", ""))),
                collection_name=str(status.get("collection_name", getattr(store, "collection_name", ""))),
                exists=bool(status.get("exists", False)),
                points_count=(int(status["points_count"]) if status.get("points_count") is not None else None),
                vector_size=(int(status["vector_size"]) if status.get("vector_size") is not None else None),
                vector_dtype=(str(status["vector_dtype"]) if status.get("vector_dtype") is not None else None),
                collection_status=(
                    str(status["collection_status"]) if status.get("collection_status") is not None else None
                ),
                error=(str(status["error"]) if status.get("error") else None),
            )
        except Exception as exc:
            return StatusImageSearchVectorStoreSummary(
                healthy=False,
                url=str(settings.qdrant.url),
                collection_name=QdrantThumbnailStore.COLLECTION_NAME,
                exists=False,
                error=str(exc),
            )

    @staticmethod
    def _indexing_status() -> StatusImageSearchIndexingSummary:
        pending = (
            MediaThumbnail.select()
            .where(MediaThumbnail.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_PENDING)
            .count()
        )
        failed = (
            MediaThumbnail.select()
            .where(MediaThumbnail.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_FAILED)
            .count()
        )
        success = (
            MediaThumbnail.select()
            .where(MediaThumbnail.image_search_index_status == MediaThumbnail.IMAGE_SEARCH_INDEX_STATUS_SUCCESS)
            .count()
        )
        return StatusImageSearchIndexingSummary(
            pending_thumbnails=int(pending),
            failed_thumbnails=int(failed),
            success_thumbnails=int(success),
        )
