import os
import time

from peewee import fn

from src.common.runtime_time import utc_now_for_db
from src.config.config import settings
from src.metadata.factory import build_dmm_provider, build_javdb_provider
from src.metadata.provider import MetadataLicenseError, MetadataNotFoundError, MetadataRequestError
from src.model import Actor, Media, MediaLibrary, MediaThumbnail, Movie
from src.service.discovery.joytag_embedder_client import (
    JoyTagInferenceClientError,
    get_joytag_embedder_client,
)
from src.service.discovery.lancedb_thumbnail_store import get_lancedb_thumbnail_store
from src.schema.system.status import (
    StatusActorSummary,
    StatusImageSearchIndexingSummary,
    StatusImageSearchResource,
    StatusJoyTagSummary,
    StatusLanceDbSummary,
    StatusMediaFileSummary,
    StatusMediaLibrarySummary,
    StatusMetadataProviderTestError,
    StatusMetadataProviderTestResource,
    StatusMovieSummary,
    StatusResource,
)


class StatusService:
    FEMALE_GENDER = 1
    BACKEND_VERSION_ENV_KEY = "SAKURAMEDIA_BACKEND_VERSION"
    BACKEND_VERSION_DEFAULT = "dev-local"
    METADATA_PROVIDER_TEST_MOVIE_NUMBER = "SSNI-888"
    METADATA_PROVIDER_DESCRIPTION_EXCERPT_LENGTH = 120

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
        joytag = cls._probe_joytag()
        lancedb = cls._probe_lancedb()
        indexing = cls._indexing_status()
        return StatusImageSearchResource(
            healthy=bool(joytag.healthy and lancedb.healthy),
            checked_at=utc_now_for_db(),
            joytag=joytag,
            lancedb=lancedb,
            indexing=indexing,
        )

    @classmethod
    def test_metadata_provider(cls, provider: str) -> StatusMetadataProviderTestResource:
        normalized_provider = provider.strip().lower()
        start_at = time.time()
        try:
            if normalized_provider == "javdb":
                return cls._test_javdb_provider(start_at=start_at)
            if normalized_provider == "dmm":
                return cls._test_dmm_provider(start_at=start_at)
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
        except MetadataLicenseError as exc:
            return cls._build_metadata_provider_failure(
                provider=normalized_provider,
                start_at=start_at,
                error=StatusMetadataProviderTestError(
                    type="metadata_license_error",
                    message=str(exc),
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
        # JavDB 联通性以真实按番号搜索并拉取详情为准，默认保持 CLI 的直连策略。
        detail = build_javdb_provider(use_metadata_proxy=False).get_movie_by_number(
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
    def _test_dmm_provider(cls, *, start_at: float) -> StatusMetadataProviderTestResource:
        # DMM 联通性以真实搜索详情页并成功解析简介为准，代理沿用统一 metadata.proxy。
        description = build_dmm_provider().get_movie_desc(cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER)
        return StatusMetadataProviderTestResource(
            healthy=True,
            checked_at=utc_now_for_db(),
            provider="dmm",
            movie_number=cls.METADATA_PROVIDER_TEST_MOVIE_NUMBER,
            elapsed_ms=cls._elapsed_ms(start_at),
            description_length=len(description),
            description_excerpt=description[: cls.METADATA_PROVIDER_DESCRIPTION_EXCERPT_LENGTH],
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
    def _probe_joytag(cls) -> StatusJoyTagSummary:
        try:
            runtime = get_joytag_embedder_client().get_runtime_status()
        except JoyTagInferenceClientError as exc:
            return StatusJoyTagSummary(
                healthy=False,
                endpoint=str(settings.image_search.inference_base_url),
                error=exc.message,
            )
        except Exception as exc:
            return StatusJoyTagSummary(
                healthy=False,
                endpoint=str(settings.image_search.inference_base_url),
                error=str(exc),
            )
        return StatusJoyTagSummary(
            healthy=True,
            endpoint=runtime.endpoint,
            backend=runtime.backend,
            execution_provider=runtime.execution_provider,
            used_device=runtime.device,
            available_devices=[str(item) for item in list(runtime.available_providers or [])],
            device_full_name=runtime.device_full_name,
            model_file=runtime.model_path,
            model_name=runtime.model_name,
            vector_size=runtime.vector_size,
            image_size=runtime.image_size,
            probe_latency_ms=runtime.probe_latency_ms,
        )

    @staticmethod
    def _probe_lancedb() -> StatusLanceDbSummary:
        try:
            store = get_lancedb_thumbnail_store()
            status = store.inspect_status()
            return StatusLanceDbSummary(
                healthy=bool(status.get("healthy", False)),
                uri=str(status.get("uri", getattr(store, "uri", ""))),
                table_name=str(status.get("table_name", getattr(store, "table_name", ""))),
                table_exists=bool(status.get("table_exists", False)),
                row_count=(int(status["row_count"]) if status.get("row_count") is not None else None),
                vector_size=(int(status["vector_size"]) if status.get("vector_size") is not None else None),
                vector_dtype=(str(status["vector_dtype"]) if status.get("vector_dtype") is not None else None),
                has_vector_index=(
                    bool(status["has_vector_index"])
                    if status.get("has_vector_index") is not None
                    else None
                ),
                error=(str(status["error"]) if status.get("error") else None),
            )
        except Exception as exc:
            return StatusLanceDbSummary(
                healthy=False,
                uri=str(settings.lancedb.uri),
                table_name=str(settings.lancedb.table_name),
                table_exists=False,
                error=str(exc),
            )

    @staticmethod
    def _indexing_status() -> StatusImageSearchIndexingSummary:
        pending = (
            MediaThumbnail.select()
            .where(MediaThumbnail.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_PENDING)
            .count()
        )
        failed = (
            MediaThumbnail.select()
            .where(MediaThumbnail.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_FAILED)
            .count()
        )
        success = (
            MediaThumbnail.select()
            .where(MediaThumbnail.joytag_index_status == MediaThumbnail.JOYTAG_INDEX_STATUS_SUCCESS)
            .count()
        )
        return StatusImageSearchIndexingSummary(
            pending_thumbnails=int(pending),
            failed_thumbnails=int(failed),
            success_thumbnails=int(success),
        )
