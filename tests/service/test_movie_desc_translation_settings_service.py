import json

import pytest
import toml

import src.config.config as config_module
from src.api.exception.errors import ApiError
from src.config.config import MovieInfoTranslation, Settings
from src.schema.system.movie_desc_translation_settings import (
    MovieDescTranslationSettingsTestRequest,
    MovieDescTranslationSettingsUpdateRequest,
)
from src.service.system.movie_desc_translation_settings_service import (
    DEFAULT_API_TEST_TRANSLATION_TEXT,
    MovieDescTranslationSettingsService,
)


@pytest.fixture()
def isolated_movie_desc_translation_settings(tmp_path, monkeypatch):
    original_runtime_settings = Settings.model_validate(
        config_module.settings.model_dump()
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        toml.dumps(json.loads(original_runtime_settings.model_dump_json())),
        encoding="utf-8",
    )
    monkeypatch.setitem(config_module.Settings.model_config, "toml_file", config_path)

    config_module.settings.movie_info_translation = MovieInfoTranslation(
        enabled=False,
        base_url="http://saved-llm:8000",
        api_key="saved-token",
        model="saved-model",
        timeout_seconds=60,
        connect_timeout_seconds=5,
    )

    yield config_path

    config_module.refresh_runtime_settings(original_runtime_settings)


def _persisted_movie_info_translation(config_path) -> dict:
    return toml.load(config_path)["movie_info_translation"]


def test_get_settings_returns_current_translation_configuration(
    isolated_movie_desc_translation_settings,
):
    response = MovieDescTranslationSettingsService.get_settings()

    assert response.model_dump() == {
        "enabled": False,
        "base_url": "http://saved-llm:8000",
        "api_key": "saved-token",
        "model": "saved-model",
        "timeout_seconds": 60.0,
        "connect_timeout_seconds": 5.0,
    }


def test_update_settings_persists_only_movie_desc_translation_section(
    isolated_movie_desc_translation_settings,
):
    original_auth_secret = config_module.settings.auth.secret_key

    response = MovieDescTranslationSettingsService.update_settings(
        MovieDescTranslationSettingsUpdateRequest(
            enabled=True,
            base_url=" http://updated-llm:9000/ ",
            api_key="  ",
            model=" updated-model ",
            timeout_seconds=120,
            connect_timeout_seconds=7,
        )
    )

    assert response.model_dump() == {
        "enabled": True,
        "base_url": "http://updated-llm:9000",
        "api_key": "",
        "model": "updated-model",
        "timeout_seconds": 120.0,
        "connect_timeout_seconds": 7.0,
    }
    assert config_module.settings.auth.secret_key == original_auth_secret
    assert _persisted_movie_info_translation(isolated_movie_desc_translation_settings) == {
        "enabled": True,
        "base_url": "http://updated-llm:9000",
        "api_key": "",
        "model": "updated-model",
        "timeout_seconds": 120.0,
        "connect_timeout_seconds": 7.0,
    }


def test_update_settings_rejects_empty_payload(
    isolated_movie_desc_translation_settings,
):
    with pytest.raises(ApiError) as exc_info:
        MovieDescTranslationSettingsService.update_settings(
            MovieDescTranslationSettingsUpdateRequest()
        )

    assert exc_info.value.code == "empty_movie_desc_translation_settings_update"


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        (
            MovieDescTranslationSettingsUpdateRequest(base_url="localhost:8000"),
            "invalid_movie_desc_translation_base_url",
        ),
        (
            MovieDescTranslationSettingsUpdateRequest(model="   "),
            "invalid_movie_desc_translation_model",
        ),
        (
            MovieDescTranslationSettingsUpdateRequest(timeout_seconds=0),
            "invalid_movie_desc_translation_timeout_seconds",
        ),
        (
            MovieDescTranslationSettingsUpdateRequest(connect_timeout_seconds=-1),
            "invalid_movie_desc_translation_connect_timeout_seconds",
        ),
    ],
)
def test_update_settings_rejects_invalid_values(
    isolated_movie_desc_translation_settings,
    payload,
    error_code,
):
    with pytest.raises(ApiError) as exc_info:
        MovieDescTranslationSettingsService.update_settings(payload)

    assert exc_info.value.code == error_code


def test_test_settings_uses_saved_config_when_request_is_empty(
    isolated_movie_desc_translation_settings,
    monkeypatch: pytest.MonkeyPatch,
):
    captured = {}

    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, timeout_seconds=None, connect_timeout_seconds=None, **kwargs):
            captured["init"] = {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "connect_timeout_seconds": connect_timeout_seconds,
            }
            self.base_url = (base_url or "").rstrip("/")
            self.model = model or ""

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            captured["translate"] = {
                "system_prompt": system_prompt,
                "source_text": source_text,
            }
            return "测试译文"

    monkeypatch.setattr(
        "src.service.system.movie_desc_translation_settings_service.MovieDescTranslationClient",
        FakeTranslationClient,
    )

    response = MovieDescTranslationSettingsService.test_settings(
        MovieDescTranslationSettingsTestRequest()
    )

    assert response.model_dump() == {"ok": True}
    assert captured["init"] == {
        "base_url": "http://saved-llm:8000",
        "api_key": "saved-token",
        "model": "saved-model",
        "timeout_seconds": 60,
        "connect_timeout_seconds": 5,
    }
    assert captured["translate"]["source_text"] == DEFAULT_API_TEST_TRANSLATION_TEXT
    assert "你是一位国内老牌成人资源站的简介撰稿人" in captured["translate"]["system_prompt"]


def test_test_settings_merges_draft_overrides_without_mutating_runtime_or_file(
    isolated_movie_desc_translation_settings,
    monkeypatch: pytest.MonkeyPatch,
):
    initial_runtime = config_module.settings.movie_info_translation.model_dump()
    initial_persisted = dict(_persisted_movie_info_translation(isolated_movie_desc_translation_settings))
    captured = {}

    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, timeout_seconds=None, connect_timeout_seconds=None, **kwargs):
            captured["init"] = {
                "base_url": base_url,
                "api_key": api_key,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "connect_timeout_seconds": connect_timeout_seconds,
            }
            self.base_url = (base_url or "").rstrip("/")
            self.model = model or ""

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            captured["translate"] = {
                "system_prompt": system_prompt,
                "source_text": source_text,
            }
            return "草稿译文"

    monkeypatch.setattr(
        "src.service.system.movie_desc_translation_settings_service.MovieDescTranslationClient",
        FakeTranslationClient,
    )

    response = MovieDescTranslationSettingsService.test_settings(
        MovieDescTranslationSettingsTestRequest(
            base_url="http://draft-llm:9000/",
            api_key="",
            model="draft-model",
            timeout_seconds=180,
            connect_timeout_seconds=9,
            text=" 自定义测试文本 ",
        )
    )

    assert response.model_dump() == {"ok": True}
    assert captured["init"] == {
        "base_url": "http://draft-llm:9000",
        "api_key": "",
        "model": "draft-model",
        "timeout_seconds": 180.0,
        "connect_timeout_seconds": 9.0,
    }
    assert config_module.settings.movie_info_translation.model_dump() == initial_runtime
    assert _persisted_movie_info_translation(isolated_movie_desc_translation_settings) == initial_persisted
    assert "你是一位国内老牌成人资源站的简介撰稿人" in captured["translate"]["system_prompt"]


def test_test_settings_maps_client_error_to_api_error(
    isolated_movie_desc_translation_settings,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, **kwargs):
            self.base_url = (base_url or "").rstrip("/")
            self.model = model or ""

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            from src.service.catalog.movie_desc_translation_client import (
                MovieDescTranslationClientError,
            )

            raise MovieDescTranslationClientError(
                503,
                "movie_desc_translation_unavailable",
                "影片简介翻译服务不可达",
            )

    monkeypatch.setattr(
        "src.service.system.movie_desc_translation_settings_service.MovieDescTranslationClient",
        FakeTranslationClient,
    )

    with pytest.raises(ApiError) as exc_info:
        MovieDescTranslationSettingsService.test_settings(
            MovieDescTranslationSettingsTestRequest(model="draft-model")
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.code == "movie_desc_translation_unavailable"
    assert exc_info.value.details == {
        "base_url": "http://saved-llm:8000",
        "model": "draft-model",
    }
