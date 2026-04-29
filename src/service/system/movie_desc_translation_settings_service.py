from urllib.parse import urlparse

from src.api.exception.errors import ApiError
from src.config.config import Settings, settings, update_settings as persist_settings
from src.schema.system.movie_desc_translation_settings import (
    MovieDescTranslationSettingsResource,
    MovieDescTranslationSettingsTestRequest,
    MovieDescTranslationSettingsTestResource,
    MovieDescTranslationSettingsUpdateRequest,
)
from src.service.catalog.movie_desc_translation_client import (
    MovieDescTranslationClient,
    MovieDescTranslationClientError,
)
from src.service.catalog.movie_desc_translation_service import MovieDescTranslationService


DEFAULT_API_TEST_TRANSLATION_TEXT = "hi"


class MovieDescTranslationSettingsService:
    @classmethod
    def get_settings(cls) -> MovieDescTranslationSettingsResource:
        return MovieDescTranslationSettingsResource.model_validate(
            settings.movie_info_translation.model_dump()
        )

    @classmethod
    def update_settings(
        cls,
        payload: MovieDescTranslationSettingsUpdateRequest,
    ) -> MovieDescTranslationSettingsResource:
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(
                422,
                "empty_movie_desc_translation_settings_update",
                "At least one field must be provided",
            )

        current_settings = Settings.model_validate(settings.model_dump())
        translation_settings = current_settings.movie_info_translation.model_copy(deep=True)

        if "enabled" in update_data:
            translation_settings.enabled = cls._validate_enabled(payload.enabled)
        if "base_url" in update_data:
            translation_settings.base_url = cls._validate_base_url(payload.base_url)
        if "api_key" in update_data:
            translation_settings.api_key = cls._validate_api_key(payload.api_key)
        if "model" in update_data:
            translation_settings.model = cls._validate_model(payload.model)
        if "timeout_seconds" in update_data:
            translation_settings.timeout_seconds = cls._validate_timeout_seconds(payload.timeout_seconds)
        if "connect_timeout_seconds" in update_data:
            translation_settings.connect_timeout_seconds = cls._validate_connect_timeout_seconds(
                payload.connect_timeout_seconds
            )

        current_settings.movie_info_translation = translation_settings
        persist_settings(current_settings)
        return cls.get_settings()

    @classmethod
    def test_settings(
        cls,
        payload: MovieDescTranslationSettingsTestRequest,
    ) -> MovieDescTranslationSettingsTestResource:
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        runtime_settings = settings.movie_info_translation

        # 测试接口只做“当前配置 + 草稿覆盖”的临时合并，不改动全局运行时配置。
        client = MovieDescTranslationClient(
            base_url=(
                cls._validate_base_url(payload.base_url)
                if "base_url" in update_data
                else runtime_settings.base_url
            ),
            api_key=(
                cls._validate_api_key(payload.api_key)
                if "api_key" in update_data
                else runtime_settings.api_key
            ),
            model=(
                cls._validate_model(payload.model)
                if "model" in update_data
                else runtime_settings.model
            ),
            timeout_seconds=(
                cls._validate_timeout_seconds(payload.timeout_seconds)
                if "timeout_seconds" in update_data
                else runtime_settings.timeout_seconds
            ),
            connect_timeout_seconds=(
                cls._validate_connect_timeout_seconds(payload.connect_timeout_seconds)
                if "connect_timeout_seconds" in update_data
                else runtime_settings.connect_timeout_seconds
            ),
        )
        source_text = cls._resolve_test_text(payload.text if "text" in update_data else None)
        system_prompt = cls._load_prompt()

        try:
            client.translate(system_prompt=system_prompt, source_text=source_text)
        except MovieDescTranslationClientError as exc:
            raise ApiError(
                exc.status_code,
                exc.error_code,
                exc.message,
                {
                    "base_url": client.base_url,
                    "model": client.model,
                },
            ) from exc

        return MovieDescTranslationSettingsTestResource(
            ok=True,
        )

    @staticmethod
    def _validate_enabled(value: bool | None) -> bool:
        if value is None:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_enabled",
                "Enabled flag cannot be empty",
            )
        return bool(value)

    @staticmethod
    def _validate_base_url(value: str | None) -> str:
        if value is None:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_base_url",
                "Base URL cannot be empty",
            )
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_base_url",
                "Base URL cannot be empty",
            )

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_base_url",
                "Base URL must use http or https",
                {"base_url": value},
            )
        return normalized.rstrip("/")

    @staticmethod
    def _validate_api_key(value: str | None) -> str:
        if value is None:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_api_key",
                "API key must be a string",
            )
        return value.strip()

    @staticmethod
    def _validate_model(value: str | None) -> str:
        if value is None:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_model",
                "Model cannot be empty",
            )
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_model",
                "Model cannot be empty",
            )
        return normalized

    @staticmethod
    def _validate_timeout_seconds(value: float | None) -> float:
        if value is None or float(value) <= 0:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_timeout_seconds",
                "timeout_seconds must be greater than 0",
                {"timeout_seconds": value},
            )
        return float(value)

    @staticmethod
    def _validate_connect_timeout_seconds(value: float | None) -> float:
        if value is None or float(value) <= 0:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_connect_timeout_seconds",
                "connect_timeout_seconds must be greater than 0",
                {"connect_timeout_seconds": value},
            )
        return float(value)

    @staticmethod
    def _resolve_test_text(value: str | None) -> str:
        if value is None:
            return DEFAULT_API_TEST_TRANSLATION_TEXT
        normalized = value.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_movie_desc_translation_test_text",
                "Test text cannot be empty",
            )
        return normalized

    @staticmethod
    def _load_prompt() -> str:
        try:
            # 测试接口直接复用正式翻译任务的 prompt 文件，确保探测语义和生产链路一致。
            return MovieDescTranslationService()._load_prompt()
        except (FileNotFoundError, ValueError) as exc:
            raise ApiError(
                500,
                "movie_desc_translation_prompt_unavailable",
                str(exc),
            ) from exc
