import json

import pytest
import toml

import src.config.config as config_module
from src.config.config import MovieInfoTranslation, Settings


def _login(client, username="account", password="password123"):
    response = client.post(
        "/auth/tokens",
        json={"username": username, "password": password},
    )
    return response.json()["access_token"]


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


def test_movie_desc_translation_settings_endpoints_require_authentication(client):
    get_response = client.get("/movie-desc-translation-settings")
    patch_response = client.patch(
        "/movie-desc-translation-settings",
        json={"model": "updated-model"},
    )
    test_response = client.post("/movie-desc-translation-settings/test", json={})

    assert get_response.status_code == 401
    assert patch_response.status_code == 401
    assert test_response.status_code == 401


def test_get_movie_desc_translation_settings_returns_current_configuration(
    client,
    account_user,
    isolated_movie_desc_translation_settings,
):
    token = _login(client, username=account_user.username)

    response = client.get(
        "/movie-desc-translation-settings",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "base_url": "http://saved-llm:8000",
        "api_key": "saved-token",
        "model": "saved-model",
        "timeout_seconds": 60.0,
        "connect_timeout_seconds": 5.0,
    }


def test_patch_movie_desc_translation_settings_updates_and_persists_values(
    client,
    account_user,
    isolated_movie_desc_translation_settings,
):
    token = _login(client, username=account_user.username)
    headers = {"Authorization": f"Bearer {token}"}

    patch_response = client.patch(
        "/movie-desc-translation-settings",
        headers=headers,
        json={
            "enabled": True,
            "base_url": "http://updated-llm:9000/",
            "api_key": "",
            "model": "updated-model",
            "timeout_seconds": 120,
            "connect_timeout_seconds": 7,
        },
    )
    get_response = client.get("/movie-desc-translation-settings", headers=headers)

    assert patch_response.status_code == 200
    assert patch_response.json() == {
        "enabled": True,
        "base_url": "http://updated-llm:9000",
        "api_key": "",
        "model": "updated-model",
        "timeout_seconds": 120.0,
        "connect_timeout_seconds": 7.0,
    }
    assert get_response.json() == patch_response.json()
    assert _persisted_movie_info_translation(isolated_movie_desc_translation_settings) == {
        "enabled": True,
        "base_url": "http://updated-llm:9000",
        "api_key": "",
        "model": "updated-model",
        "timeout_seconds": 120.0,
        "connect_timeout_seconds": 7.0,
    }


def test_patch_movie_desc_translation_settings_rejects_empty_payload(
    client,
    account_user,
    isolated_movie_desc_translation_settings,
):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/movie-desc-translation-settings",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "empty_movie_desc_translation_settings_update"


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ({"base_url": "localhost:8000"}, "invalid_movie_desc_translation_base_url"),
        ({"model": "   "}, "invalid_movie_desc_translation_model"),
        ({"timeout_seconds": 0}, "invalid_movie_desc_translation_timeout_seconds"),
        (
            {"connect_timeout_seconds": -1},
            "invalid_movie_desc_translation_connect_timeout_seconds",
        ),
    ],
)
def test_patch_movie_desc_translation_settings_rejects_invalid_values(
    client,
    account_user,
    isolated_movie_desc_translation_settings,
    payload,
    error_code,
):
    token = _login(client, username=account_user.username)

    response = client.patch(
        "/movie-desc-translation-settings",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == error_code


def test_post_movie_desc_translation_settings_test_uses_saved_config(
    client,
    account_user,
    isolated_movie_desc_translation_settings,
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, **kwargs):
            self.base_url = (base_url or "").rstrip("/")
            self.model = model or ""

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            assert source_text == "hi"
            assert "你是一位国内老牌成人资源站的简介撰稿人" in system_prompt
            return "测试译文"

    monkeypatch.setattr(
        "src.service.system.movie_desc_translation_settings_service.MovieDescTranslationClient",
        FakeTranslationClient,
    )
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movie-desc-translation-settings/test",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_post_movie_desc_translation_settings_test_supports_draft_overrides_without_persisting(
    client,
    account_user,
    isolated_movie_desc_translation_settings,
    monkeypatch: pytest.MonkeyPatch,
):
    initial_runtime = config_module.settings.movie_info_translation.model_dump()
    initial_persisted = dict(_persisted_movie_info_translation(isolated_movie_desc_translation_settings))

    class FakeTranslationClient:
        def __init__(self, *, base_url=None, api_key=None, model=None, timeout_seconds=None, connect_timeout_seconds=None, **kwargs):
            assert base_url == "http://draft-llm:9000"
            assert api_key == ""
            assert model == "draft-model"
            assert timeout_seconds == 180.0
            assert connect_timeout_seconds == 9.0
            self.base_url = base_url or ""
            self.model = model or ""

        def translate(self, *, system_prompt: str, source_text: str) -> str:
            assert source_text == "自定义文本"
            assert "你是一位国内老牌成人资源站的简介撰稿人" in system_prompt
            return "草稿测试译文"

    monkeypatch.setattr(
        "src.service.system.movie_desc_translation_settings_service.MovieDescTranslationClient",
        FakeTranslationClient,
    )
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movie-desc-translation-settings/test",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "base_url": "http://draft-llm:9000/",
            "api_key": "",
            "model": "draft-model",
            "timeout_seconds": 180,
            "connect_timeout_seconds": 9,
            "text": " 自定义文本 ",
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert config_module.settings.movie_info_translation.model_dump() == initial_runtime
    assert _persisted_movie_info_translation(isolated_movie_desc_translation_settings) == initial_persisted


def test_post_movie_desc_translation_settings_test_maps_client_error_payload(
    client,
    account_user,
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
    token = _login(client, username=account_user.username)

    response = client.post(
        "/movie-desc-translation-settings/test",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "draft-model"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "movie_desc_translation_unavailable",
            "message": "影片简介翻译服务不可达",
            "details": {
                "base_url": "http://saved-llm:8000",
                "model": "draft-model",
            },
        }
    }
