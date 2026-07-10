import time
from typing import List, Optional, Set
from urllib.parse import urlparse

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.config.config import (
    IndexerKind,
    IndexerType,
    Settings,
    settings,
    update_settings as persist_settings,
)
from src.model import DownloadClient, Indexer
from src.schema.system.indexer_settings import (
    IndexerConnectionTestError,
    IndexerConnectionTestResponse,
    IndexerItemUpdatePayload,
    IndexerItemResource,
    IndexerSettingsResource,
    IndexerSettingsUpdateRequest,
)


class IndexerSettingsService:
    # 连通性测试固定用一个已知存在的番号做真实搜索，比单纯 ping 更能反映 apikey/indexer 是否真正可用。
    CONNECTION_TEST_QUERY = "SSNI-888"

    @staticmethod
    def get_settings() -> IndexerSettingsResource:
        return IndexerSettingsResource(
            type=settings.indexer_settings.type,
            api_key=settings.indexer_settings.api_key,
            indexers=[
                IndexerItemResource(
                    id=indexer.id,
                    name=indexer.name,
                    url=indexer.url,
                    kind=IndexerKind(indexer.kind),
                    download_client_id=indexer.download_client_id,
                    download_client_name=indexer.download_client.name,
                )
                for indexer in (
                    Indexer.select(Indexer, DownloadClient)
                    .join(DownloadClient)
                    .order_by(Indexer.id.asc())
                )
            ],
        )

    @classmethod
    def update_settings(
        cls,
        payload: IndexerSettingsUpdateRequest,
    ) -> IndexerSettingsResource:
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422,
                "empty_indexer_settings_update",
                "At least one field must be provided",
            )

        current_settings = Settings.model_validate(settings.model_dump())
        indexer_settings = current_settings.indexer_settings.model_copy(deep=True)

        if "type" in update_data:
            indexer_settings.type = cls._validate_type(payload.type)

        if "api_key" in update_data:
            indexer_settings.api_key = cls._validate_api_key(payload.api_key)

        if "indexers" in update_data:
            cls._replace_indexers(payload.indexers)

        current_settings.indexer_settings = indexer_settings
        persist_settings(current_settings)
        return cls.get_settings()

    @classmethod
    def test_connection(
        cls,
        *,
        jackett_client_cls=None,
    ) -> IndexerConnectionTestResponse:
        """Jackett 连通性检测:用固定番号真实搜一次，而不是单纯 ping,以此验证 apikey 与 indexer 配置整体可用。"""
        # 延迟导入，避免 system service 与 transfers service 顶层依赖链形成循环。
        from src.service.transfers.jackett_client import JackettClient, JackettClientError

        jackett_client_cls = jackett_client_cls or JackettClient
        start_at = time.time()
        indexers_checked = Indexer.select().count()
        if indexers_checked == 0:
            return cls._build_connection_test_response(
                start_at=start_at,
                healthy=False,
                indexers_checked=0,
                error=IndexerConnectionTestError(
                    type="no_indexers_configured",
                    message="尚未配置任何 indexer，无法测试 Jackett 连通性",
                ),
            )

        try:
            candidates = jackett_client_cls().search(cls.CONNECTION_TEST_QUERY)
        except JackettClientError as exc:
            return cls._build_connection_test_response(
                start_at=start_at,
                healthy=False,
                indexers_checked=indexers_checked,
                error=IndexerConnectionTestError(
                    type="jackett_request_error",
                    message=str(exc),
                ),
            )

        return cls._build_connection_test_response(
            start_at=start_at,
            healthy=True,
            indexers_checked=indexers_checked,
            result_count=len(candidates),
        )

    @classmethod
    def _build_connection_test_response(
        cls,
        *,
        start_at: float,
        healthy: bool,
        indexers_checked: int,
        result_count: int = 0,
        error: Optional[IndexerConnectionTestError] = None,
    ) -> IndexerConnectionTestResponse:
        return IndexerConnectionTestResponse(
            healthy=healthy,
            checked_at=utc_now_for_db(),
            query=cls.CONNECTION_TEST_QUERY,
            indexers_checked=indexers_checked,
            result_count=result_count,
            elapsed_ms=int((time.time() - start_at) * 1000),
            error=error,
        )

    @staticmethod
    def _validate_api_key(value: Optional[str]) -> str:
        if value is None:
            raise ApiError(
                422,
                "invalid_indexer_settings_api_key",
                "Indexer API key cannot be empty",
            )
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_api_key",
                "Indexer API key cannot be empty",
            )
        return normalized

    @staticmethod
    def _validate_name(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_name",
                "Indexer name cannot be empty",
            )
        return normalized

    @staticmethod
    def _validate_url(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_url",
                "Indexer URL cannot be empty",
            )

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ApiError(
                422,
                "invalid_indexer_settings_url",
                "Indexer URL must use http or https",
                {"url": value},
            )
        return normalized

    @staticmethod
    def _validate_type(value: Optional[str]) -> IndexerType:
        if value is None:
            raise ApiError(
                422,
                "invalid_indexer_settings_type",
                "Indexer type cannot be empty",
            )
        normalized = value.strip().lower()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_type",
                "Indexer type cannot be empty",
            )

        try:
            return IndexerType(normalized)
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_indexer_settings_type",
                "Unsupported indexer type",
                {"type": value},
            ) from exc

    @staticmethod
    def _validate_kind(value: str) -> IndexerKind:
        normalized = value.strip().lower()
        if not normalized:
            raise ApiError(
                422,
                "invalid_indexer_settings_kind",
                "Indexer kind cannot be empty",
            )

        try:
            return IndexerKind(normalized)
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_indexer_settings_kind",
                "Unsupported indexer kind",
                {"kind": value},
            ) from exc

    @classmethod
    def _validate_indexers(
        cls,
        items: Optional[List[IndexerItemUpdatePayload]],
    ) -> List[dict]:
        if items is None:
            raise ApiError(
                422,
                "invalid_indexer_settings_indexers",
                "Indexers must be a list",
            )
        normalized_names: Set[str] = set()
        indexers: List[dict] = []

        for item in items:
            name = cls._validate_name(item.name)
            normalized_name = name.casefold()
            if normalized_name in normalized_names:
                raise ApiError(
                    422,
                    "duplicate_indexer_settings_name",
                    "Indexer name must be unique",
                    {"name": name},
                )
            normalized_names.add(normalized_name)
            indexers.append(
                {
                    "name": name,
                    "url": cls._validate_url(item.url),
                    "kind": cls._validate_kind(item.kind).value,
                    "download_client_id": cls._validate_download_client_id(item.download_client_id),
                }
            )

        return indexers

    @staticmethod
    def _validate_download_client_id(value: int) -> int:
        if value <= 0:
            raise ApiError(
                422,
                "invalid_indexer_settings_download_client_id",
                "download_client_id must be a positive integer",
                {"download_client_id": value},
            )
        client = DownloadClient.get_or_none(DownloadClient.id == value)
        if client is None:
            raise ApiError(
                404,
                "indexer_settings_download_client_not_found",
                "Download client not found",
                {"download_client_id": value},
            )
        return client.id

    @classmethod
    def _replace_indexers(cls, items: Optional[List[IndexerItemUpdatePayload]]) -> None:
        validated_items = cls._validate_indexers(items)
        with Indexer._meta.database.atomic():
            Indexer.delete().execute()
            if validated_items:
                Indexer.insert_many(validated_items).execute()
