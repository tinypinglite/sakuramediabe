"""配置接口契约测试：隔离鉴权和数据库依赖，不连接数据库。"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.exception.errors import ApiError
from src.api.exception.exception import api_error_handler
from src.api.routers.deps import db_deps, get_current_user
from src.api.routers.system.plugins import router
from src.config.config import settings
from src.plugins.loader import _clear_plugin_modules


@pytest.fixture
def settings_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "plugins", settings.plugins.model_copy(deep=True))
    settings.plugins.settings = {"form_plugin": {"retry_days": 0}}
    monkeypatch.setattr("src.plugins.manager._plugin_root", lambda: tmp_path)
    monkeypatch.setattr(
        "src.plugins.manager.update_settings",
        lambda updated: setattr(settings, "plugins", updated.plugins),
    )
    for name in ("form_plugin", "legacy_plugin"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({
            "plugin_id": name, "display_name": name, "version": "1.0.0",
            "host_api_version": 6,
            **({"settings_model": "Settings"} if name == "form_plugin" else {}),
        }))
        (directory / "__init__.py").write_text(
            'from pydantic import BaseModel, Field\n'
            'class Settings(BaseModel):\n'
            '    retry_days: int = Field(default=3, ge=1, le=365, title="重试间隔")\n'
            'def register(context):\n'
            '    raise RuntimeError("配置无效，注册失败")\n'
            if name == "form_plugin" else 'raise RuntimeError("旧插件不应被导入")\n'
        )
    directory = tmp_path / "complex_plugin"
    directory.mkdir()
    (directory / "manifest.json").write_text(json.dumps({
        "plugin_id": "complex_plugin", "display_name": "配置协议测试", "version": "1.0.0",
        "host_api_version": 6, "settings_model": "Settings",
    }))
    (directory / "__init__.py").write_text('''from pydantic import BaseModel, ConfigDict, Field, model_validator
class Nested(BaseModel):
    retries: int = Field(default=2, ge=0)
    prefixes: set[str] = Field(default_factory=lambda: {"AA", "BB"})
class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    nested: Nested
    password: str | None = Field(default=None, json_schema_extra={"format": "password"})
    nullable_default: str | None = "default"
    enabled: bool = False
    @model_validator(mode="after")
    def validate_enabled(self):
        if self.enabled and not self.password:
            raise ValueError("启用时请填写密码")
        return self
''')
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(ApiError, api_error_handler)
    app.dependency_overrides[db_deps] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: object()
    with TestClient(app) as client:
        yield client
    _clear_plugin_modules("form_plugin")
    _clear_plugin_modules("complex_plugin")


def test_schema_available_with_invalid_settings_and_failed_register(settings_client):
    response = settings_client.get('/system/plugins/form_plugin/settings')
    assert response.status_code == 200
    assert response.json()['settings'] == {'retry_days': 0}
    field = response.json()['schema']['properties']['retry_days']
    assert field['default'] == 3
    assert field['minimum'] == 1
    assert field['title'] == '重试间隔'


def test_validate_before_write_and_return_normalized_defaults(settings_client):
    path = '/system/plugins/form_plugin/settings'
    response = settings_client.put(path, json={'retry_days': 366})
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'invalid_plugin_settings'
    assert settings.plugins.settings['form_plugin'] == {'retry_days': 0}
    response = settings_client.put(path, json={})
    assert response.status_code == 200
    assert response.json() == {'settings': {'retry_days': 3}, 'pending_restart': ['api', 'aps']}
    assert settings.plugins.settings['form_plugin'] == {'retry_days': 3}


def test_legacy_plugin_keeps_json_contract_without_import(settings_client):
    path = '/system/plugins/legacy_plugin/settings'
    assert settings_client.get(path).json() == {'settings': {}}
    payload = {'custom': {'tags': ['a', 'b']}}
    assert settings_client.put(path, json=payload).json()['settings'] == payload
    assert settings_client.get(path).json() == {'settings': payload}
    assert settings_client.put(path, json={'custom': None}).status_code == 422
    assert settings_client.get(path).json() == {'settings': payload}
    assert settings_client.get('/system/plugins/missing/settings').status_code == 404


def test_required_fields_do_not_prevent_defaults_and_schema(settings_client):
    result = settings_client.get('/system/plugins/complex_plugin/settings')
    assert result.status_code == 200
    body = result.json()
    assert body['settings'] == {}
    assert 'name' not in body['defaults']
    assert set(body['defaults']['nested']['prefixes']) == {'AA', 'BB'}
    assert body['defaults']['nested']['retries'] == 2
    assert body['defaults']['password'] is None
    assert body['schema']['properties']['password']['format'] == 'password'


def test_nested_errors_and_model_errors_do_not_write(settings_client):
    path = '/system/plugins/complex_plugin/settings'
    response = settings_client.put(path, json={'name': 'test', 'nested': {'retries': -1}})
    assert response.status_code == 422
    assert response.json()['error']['details']['fields'][0]['path'] == ['nested', 'retries']
    response = settings_client.put(path, json={'name': 'test', 'nested': {}, 'enabled': True})
    assert response.status_code == 422
    assert response.json()['error']['details']['fields'][0]['path'] == []
    assert settings_client.get(path).json()['settings'] == {}


def test_nullable_values_round_trip_without_changing_defaults(settings_client):
    import toml

    path = '/system/plugins/complex_plugin/settings'
    response = settings_client.put(path, json={'name': 'test', 'nested': {}, 'password': None})
    assert response.status_code == 200
    saved = response.json()['settings']
    assert 'password' not in saved
    assert saved['nullable_default'] == 'default'
    assert set(saved['nested']['prefixes']) == {'AA', 'BB'}
    assert toml.loads(toml.dumps(saved)) == saved
    assert settings_client.get(path).json()['settings'] == saved
    response = settings_client.put(path, json={**saved, 'nullable_default': None})
    assert response.status_code == 422
    assert settings_client.get(path).json()['settings'] == saved


def test_set_order_does_not_change_configuration(settings_client):
    from itertools import permutations

    path = '/system/plugins/complex_plugin/settings'
    prefixes = {'OFJE', 'REBD', 'CJOB', 'DVAJ'}
    for order in permutations(prefixes):
        response = settings_client.put(path, json={'name': 'test', 'nested': {'prefixes': order}})
        assert response.status_code == 200
        assert set(response.json()['settings']['nested']['prefixes']) == prefixes
