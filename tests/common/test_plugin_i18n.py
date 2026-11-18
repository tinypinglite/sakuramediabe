import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.config.config import settings
from src.i18n.catalog import translate
from src.i18n.locale import (
    DEFAULT_LANGUAGE,
    LanguageState,
    current_language,
    reset_language_state,
    set_language_state,
)
from src.model import User
from src.plugins import HOST_API_VERSION, PluginContext
from src.plugins.i18n import (
    PluginI18n,
    localize_config_field,
    localize_json_schema,
    localize_provider_bundle,
    resolve_text,
    validate_plugin_catalogs,
)
from src.plugins.loader import (
    PluginLoadError,
    _load_error_entry,
    check_plugin_dir,
    render_plugin_load_error,
)
from src.plugins.manager import PluginManager
from src.plugins.manifest import load_manifest_from_dict
from src.plugins.provider_protocol import ConfigField
from src.service.system.language_service import task_language_scope


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def test_resolve_text_uses_locale_then_default_then_original():
    with _language("en"):
        assert resolve_text("中文", {"en": "English", "zh-CN": "中文"}) == "English"
        assert resolve_text("中文", {"zh-CN": "中文"}) == "中文"
        assert resolve_text("中文", {}) == "中文"


def test_localize_json_schema_does_not_mutate_source():
    schema = {
        "type": "object",
        "title_i18n": {"en": "Settings", "zh-CN": "设置"},
        "properties": {
            "retry_days": {
                "type": "integer",
                "title": "重试间隔",
                "title_i18n": {"en": "Retry interval"},
                "description": "天数",
                "description_i18n": {"en": "Days"},
            }
        },
    }

    with _language("en"):
        localized = localize_json_schema(schema)

    assert localized["title"] == "Settings"
    assert localized["properties"]["retry_days"]["title"] == "Retry interval"
    assert localized["properties"]["retry_days"]["description"] == "Days"
    assert "title_i18n" not in localized
    assert "title_i18n" not in localized["properties"]["retry_days"]
    # 原始 schema 保持不变，避免污染 Pydantic 缓存。
    assert schema["title_i18n"] == {"en": "Settings", "zh-CN": "设置"}
    assert schema["properties"]["retry_days"]["title"] == "重试间隔"


def test_manifest_accepts_optional_display_name_i18n():
    manifest = load_manifest_from_dict(
        {
            "plugin_id": "demo",
            "display_name": "演示插件",
            "display_name_i18n": {"en": "Demo plugin"},
            "version": "1.0.0",
            "host_api_version": 11,
        }
    )

    assert manifest.display_name_i18n == {"en": "Demo plugin"}


def test_manifest_accepts_optional_description_i18n():
    manifest = load_manifest_from_dict(
        {
            "plugin_id": "demo",
            "display_name": "演示插件",
            "description": "演示插件的用途说明",
            "description_i18n": {"en": "What the demo plugin does"},
            "version": "1.0.0",
            "host_api_version": 11,
        }
    )

    assert manifest.description == "演示插件的用途说明"
    assert manifest.description_i18n == {"en": "What the demo plugin does"}


def test_manifest_description_is_optional():
    manifest = load_manifest_from_dict(
        {
            "plugin_id": "demo",
            "display_name": "演示插件",
            "version": "1.0.0",
            "host_api_version": 11,
        }
    )

    assert manifest.description is None
    assert manifest.description_i18n == {}


def test_plugin_manager_list_plugins_localizes_display_name(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "plugins", settings.plugins.model_copy(deep=True))
    settings.plugins.enabled = []
    plugin_dir = tmp_path / "demo"
    plugin_dir.mkdir()
    (plugin_dir / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "demo",
                "display_name": "演示插件",
                "display_name_i18n": {"en": "Demo plugin"},
                "version": "1.0.0",
                "host_api_version": 11,
            }
        ),
        encoding="utf-8",
    )

    with _language("en"):
        plugins = PluginManager(root_dir=tmp_path).list_plugins()
    assert plugins[0]["display_name"] == "Demo plugin"

    with _language("zh-CN"):
        plugins = PluginManager(root_dir=tmp_path).list_plugins()
    assert plugins[0]["display_name"] == "演示插件"


def _write_plugin_catalogs(root):
    i18n_dir = root / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "zh-CN.json").write_text(
        json.dumps(
            {
                "hello": "你好 {count}",
                "items": {"one": "{count} 部", "other": "{count} 部"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (i18n_dir / "en.json").write_text(
        json.dumps(
            {
                "hello": "Hello {count}",
                "items": {"one": "{count} item", "other": "{count} items"},
            }
        ),
        encoding="utf-8",
    )


def test_plugin_i18n_loads_bundled_catalogs_and_plural(tmp_path):
    _write_plugin_catalogs(tmp_path)
    i18n = PluginI18n("demo", tmp_path)

    with _language("en"):
        assert i18n.t("hello", count=2) == "Hello 2"
        assert i18n.t("items", count=1) == "1 item"
        assert i18n.t("items", count=3) == "3 items"
        assert i18n.t("missing", default="fallback") == "fallback"
    with _language("zh-CN"):
        assert i18n.t("hello", count=2) == "你好 2"


def test_validate_plugin_catalogs_accepts_missing_and_valid_files(tmp_path):
    assert validate_plugin_catalogs(tmp_path) == []

    _write_plugin_catalogs(tmp_path)
    assert validate_plugin_catalogs(tmp_path) == []

    # 缺失语言文件合法，只检查 SUPPORTED_LANGUAGES 中实际存在的文件。
    (tmp_path / "i18n" / "en.json").unlink()
    assert validate_plugin_catalogs(tmp_path) == []


def test_validate_plugin_catalogs_reports_structure_errors(tmp_path):
    i18n_dir = tmp_path / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "en.json").write_text(
        json.dumps(
            {
                "": "empty key",
                "nested.value": {"deep": "not a plural form"},
                "bad.plural": {"one": 1, "other": "items"},
                "bad.type": ["list"],
                "valid": "ok",
                "valid.plural": {"one": "one", "other": "other"},
            }
        ),
        encoding="utf-8",
    )
    (i18n_dir / "zh-CN.json").write_text("[1, 2]", encoding="utf-8")

    errors = validate_plugin_catalogs(tmp_path)

    assert len(errors) == 5
    assert any("i18n/en.json" in error and "非空点分字符串" in error for error in errors)
    assert any("nested.value" in error for error in errors)
    assert any("bad.plural" in error and "one" in error for error in errors)
    assert any("bad.type" in error for error in errors)
    assert any("i18n/zh-CN.json" in error and "object" in error for error in errors)
    assert not any("valid" in error for error in errors)


def test_validate_plugin_catalogs_reports_invalid_json(tmp_path):
    i18n_dir = tmp_path / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "en.json").write_text("{not json", encoding="utf-8")

    errors = validate_plugin_catalogs(tmp_path)

    assert len(errors) == 1
    assert "i18n/en.json" in errors[0]
    assert "JSON" in errors[0]


def _has_cjk(text: str) -> bool:
    return re.search(r"[\u4e00-\u9fff]", text) is not None


def test_validate_plugin_catalogs_renders_details_in_request_language(tmp_path):
    i18n_dir = tmp_path / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "en.json").write_text(
        json.dumps(
            {
                "": "empty key",
                "nested.value": {"deep": "not a plural form"},
                "bad.plural": {"one": 1, "other": "items"},
            }
        ),
        encoding="utf-8",
    )

    with _language("en"):
        errors = validate_plugin_catalogs(tmp_path)

    assert len(errors) == 3
    assert any("i18n/en.json" in error and "nested.value" in error for error in errors)
    assert any("bad.plural" in error and "one" in error for error in errors)
    # 英文界面不出现中文明细。
    assert not any(_has_cjk(error) for error in errors)


def _make_plugin_dir(root):
    pkg = root / "demo_plugin"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(
        json.dumps(
            {
                "plugin_id": "demo_plugin",
                "display_name": "演示插件",
                "version": "1.0.0",
                "host_api_version": HOST_API_VERSION,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (pkg / "__init__.py").write_text(
        "from src.plugins import HOST_API_VERSION, PluginRegistration\n"
        "def register(context):\n"
        "    return PluginRegistration(plugin_id='demo_plugin', "
        "display_name='演示插件', version='1.0.0', "
        "host_api_version=HOST_API_VERSION, jobs=())\n",
        encoding="utf-8",
    )
    return pkg


def test_check_plugin_dir_reports_catalog_errors_with_host_key(tmp_path):
    plugin_dir = _make_plugin_dir(tmp_path)
    i18n_dir = plugin_dir / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "en.json").write_text(
        json.dumps({"nested.value": {"deep": "not a plural form"}}),
        encoding="utf-8",
    )

    with _language("en"), pytest.raises(PluginLoadError) as excinfo:
        check_plugin_dir(plugin_dir=plugin_dir)

    error = excinfo.value
    assert error.stage == "i18n"
    assert error.message_key == "plugins.load_error.i18n_catalog_invalid"
    details = error.message_params["errors"]
    assert "nested.value" in details
    assert not _has_cjk(details)

    with _language("en"):
        rendered = render_plugin_load_error(_load_error_entry(error))

    assert rendered.startswith("Plugin load failed")
    assert "Invalid plugin i18n catalog:" in rendered
    assert "nested.value" in rendered
    assert not _has_cjk(rendered)


def test_check_plugin_dir_reports_catalog_errors_in_default_language(tmp_path):
    plugin_dir = _make_plugin_dir(tmp_path)
    i18n_dir = plugin_dir / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "en.json").write_text(
        json.dumps({"nested.value": {"deep": "not a plural form"}}),
        encoding="utf-8",
    )

    with pytest.raises(PluginLoadError) as excinfo:
        check_plugin_dir(plugin_dir=plugin_dir)

    error = excinfo.value
    assert error.message_key == "plugins.load_error.i18n_catalog_invalid"
    # 明细在校验时按当前语言渲染；默认语言是中文。
    details = error.message_params["errors"]
    assert "nested.value" in details
    assert _has_cjk(details)

    zh_rendered = render_plugin_load_error(_load_error_entry(error))
    with _language("en"):
        en_rendered = render_plugin_load_error(_load_error_entry(error))

    # 总结文案在渲染时按当前语言走宿主语言包。
    assert zh_rendered.startswith(
        translate("plugins.load_error.summary", locale="zh-CN")
    )
    assert en_rendered.startswith(
        translate("plugins.load_error.summary", locale="en")
    )
    assert zh_rendered != en_rendered


def test_plugin_i18n_runtime_tolerates_invalid_catalog(tmp_path):
    i18n_dir = tmp_path / "i18n"
    i18n_dir.mkdir()
    (i18n_dir / "en.json").write_text("{not json", encoding="utf-8")

    with _language("en"):
        assert PluginI18n("demo", tmp_path).t("hello", default="fallback") == "fallback"


def test_plugin_context_exposes_i18n(tmp_path):
    _write_plugin_catalogs(tmp_path)
    context = PluginContext(
        plugin_id="demo",
        settings={},
        data_dir=tmp_path / "data",
        plugin_root=tmp_path,
    )

    with _language("en"):
        assert context.i18n.t("hello", count=1) == "Hello 1"
        assert context.i18n.t("missing", default="fallback") == "fallback"


def test_localize_provider_bundle_falls_back_for_old_plugins():
    with _language("en"):
        assert (
            localize_provider_bundle(
                SimpleNamespace(
                    display_name="本地媒体库",
                    display_name_i18n={"en": "Local library"},
                )
            )
            == "Local library"
        )
        assert (
            localize_provider_bundle(SimpleNamespace(display_name="本地媒体库"))
            == "本地媒体库"
        )


def test_localize_config_field_resolves_and_removes_i18n_keys():
    field = ConfigField(
        key="token",
        label="令牌",
        input="secret",
        required=True,
        description="说明",
        hint="提示",
        label_i18n={"en": "Token"},
        description_i18n={"en": "Description"},
        hint_i18n={"en": "Hint"},
    )

    with _language("en"):
        data = localize_config_field(field)

    assert data["label"] == "Token"
    assert data["description"] == "Description"
    assert data["hint"] == "Hint"
    assert "label_i18n" not in data
    assert "description_i18n" not in data
    assert "hint_i18n" not in data


def test_localize_config_field_preserves_missing_optional_text():
    field = ConfigField(
        key="token",
        label="令牌",
        input="secret",
        required=True,
        description_i18n={"en": "Description"},
    )

    with _language("zh-CN"):
        data = localize_config_field(field)

    assert data["description"] is None
    assert "description_i18n" not in data


def test_localize_config_field_ignores_malformed_i18n_map():
    field = ConfigField(
        key="token",
        label="令牌",
        input="secret",
        required=True,
        label_i18n="Token",  # type: ignore[arg-type]
    )

    data = localize_config_field(field)

    assert data["label"] == "令牌"
    assert "label_i18n" not in data


def test_plugin_i18n_follows_task_language_scope_without_leaking(tmp_path, test_db):
    _write_plugin_catalogs(tmp_path)
    user = User.create(username="task", password_hash="x", language="en")
    i18n = PluginI18n("demo", tmp_path)

    assert current_language() == DEFAULT_LANGUAGE
    with task_language_scope():
        assert current_language() == "en"
        assert i18n.t("hello", count=1) == "Hello 1"
    assert current_language() == DEFAULT_LANGUAGE

    User.update(language="zh-CN").where(User.id == user.id).execute()
    with task_language_scope():
        assert current_language() == "zh-CN"
        assert i18n.t("hello", count=1) == "你好 1"
    assert current_language() == DEFAULT_LANGUAGE
