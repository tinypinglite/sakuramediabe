"""Validator for the single ``media.provider`` bundle extension."""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from src.plugins.contracts import PluginExtension
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_EXTENSION_KEY,
    ConfigField,
    LibraryLoginSpec,
    MediaProviderBundle,
    WebLoginCookieCapture,
    WebLoginLocalStorageCapture,
)


def _validate_i18n_map(value: object, *, path: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise TypeError(f"{path} 必须是字典或 None")
    for locale, text in value.items():
        if not isinstance(locale, str) or not locale.strip():
            raise ValueError(f"{path} 包含无效语言键")
        if not isinstance(text, str):
            raise TypeError(f"{path} 的文本必须是字符串")


def _validate_config_fields(
    fields: object,
    *,
    owner: str,
) -> tuple[ConfigField, ...]:
    if not isinstance(fields, tuple):
        raise TypeError(f"{owner}.config_fields 必须是 tuple")
    seen: set[str] = set()
    for field in fields:
        if not isinstance(field, ConfigField):
            raise TypeError(f"{owner}.config_fields 必须只包含 ConfigField")
        if not isinstance(field.key, str) or not field.key or field.key.strip() != field.key:
            raise ValueError(f"{owner}.config_fields 包含无效 key")
        if field.key in seen:
            raise ValueError(f"{owner}.config_fields key 重复: {field.key}")
        seen.add(field.key)
        if not isinstance(field.label, str) or not field.label or field.label.strip() != field.label:
            raise ValueError(f"{owner}.config_fields 包含无效 label")
        if field.input not in {"text", "secret", "path"}:
            raise ValueError(f"{owner}.config_fields input 不受支持: {field.input}")
        if not isinstance(field.required, bool):
            raise TypeError(f"{owner}.config_fields.required 必须是 bool")
        if field.description is not None and not isinstance(field.description, str):
            raise TypeError(f"{owner}.config_fields.description 必须是字符串或 None")
        if not isinstance(field.multiline, bool) or not isinstance(field.read_only, bool):
            raise TypeError(f"{owner}.config_fields.multiline/read_only 必须是 bool")
        if field.hint is not None and not isinstance(field.hint, str):
            raise TypeError(f"{owner}.config_fields.hint 必须是字符串或 None")
        for name in ("label_i18n", "description_i18n", "hint_i18n"):
            _validate_i18n_map(
                getattr(field, name),
                path=f"{owner}.config_fields.{field.key}.{name}",
            )
    return fields


def _require_methods(value: object, methods: Iterable[str], *, owner: str) -> None:
    missing = [name for name in methods if not callable(getattr(value, name, None))]
    if missing:
        raise TypeError(f"{owner} 缺少必需操作: {', '.join(missing)}")


def _validate_playback_deliveries(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError("media provider playback_deliveries 必须是 tuple")
    if not value:
        raise ValueError("media provider playback_deliveries 不能为空")
    if any(delivery not in {"proxy", "redirect"} for delivery in value):
        raise ValueError("media provider playback_deliveries 包含不支持的方式")
    if len(set(value)) != len(value):
        raise ValueError("media provider playback_deliveries 不可重复")
    if "proxy" not in value:
        raise ValueError("media provider playback_deliveries 必须包含 proxy")
    return value


def _validate_merged_playback_format(value: object) -> None:
    if value is None:
        return
    if value not in {"mp4", "hls"}:
        raise ValueError("media provider merged_playback_format 必须是 mp4 或 hls")


def _validate_https_url(value: object, *, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ValueError(f"{path} 必须是有效的 HTTPS URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{path} 必须是有效的 HTTPS URL")
    return value


def _validate_https_origin(value: object, *, path: str) -> str:
    _validate_https_url(value, path=path)
    parsed = urlsplit(value)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{path} 只能包含 HTTPS origin")
    return value.rstrip("/")


def _validate_string_tuple(
    value: object,
    *,
    path: str,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"{path} 必须是 tuple")
    if not value and not allow_empty:
        raise ValueError(f"{path} 不能为空")
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or item.strip() != item:
            raise ValueError(f"{path} 包含无效字符串")
        if item in seen:
            raise ValueError(f"{path} 包含重复项: {item}")
        seen.add(item)
    return value


def _validate_library_login(
    value: object,
    *,
    config_field_keys: set[str],
) -> None:
    if value is None:
        return
    if not isinstance(value, LibraryLoginSpec):
        raise TypeError("media provider library_login 必须是 LibraryLoginSpec 或 None")
    if value.mode != "webview":
        raise ValueError("media provider library_login.mode 不受支持")
    _validate_https_url(value.start_url, path="media provider library_login.start_url")
    allowed_hosts = _validate_string_tuple(
        value.allowed_hosts,
        path="media provider library_login.allowed_hosts",
    )
    for host in allowed_hosts:
        if (
            "://" in host
            or "/" in host
            or ":" in host
            or host.startswith(".")
            or host.endswith(".")
        ):
            raise ValueError("media provider library_login.allowed_hosts 包含无效主机名")

    if not isinstance(value.cookie_captures, tuple):
        raise TypeError("media provider library_login.cookie_captures 必须是 tuple")
    for index, capture in enumerate(value.cookie_captures):
        path = f"media provider library_login.cookie_captures[{index}]"
        if not isinstance(capture, WebLoginCookieCapture):
            raise TypeError(f"{path} 必须是 WebLoginCookieCapture")
        _validate_https_url(capture.url, path=f"{path}.url")
        capture_names = _validate_string_tuple(
            capture.capture_names,
            path=f"{path}.capture_names",
        )
        required_names = _validate_string_tuple(
            capture.required_names,
            path=f"{path}.required_names",
            allow_empty=True,
        )
        if not set(required_names).issubset(capture_names):
            raise ValueError(f"{path}.required_names 必须属于 capture_names")

    if not isinstance(value.local_storage_captures, tuple):
        raise TypeError(
            "media provider library_login.local_storage_captures 必须是 tuple"
        )
    for index, capture in enumerate(value.local_storage_captures):
        path = f"media provider library_login.local_storage_captures[{index}]"
        if not isinstance(capture, WebLoginLocalStorageCapture):
            raise TypeError(f"{path} 必须是 WebLoginLocalStorageCapture")
        _validate_https_origin(capture.origin, path=f"{path}.origin")
        _validate_string_tuple(
            capture.required_json_keys,
            path=f"{path}.required_json_keys",
            allow_empty=True,
        )

    managed_keys = _validate_string_tuple(
        value.managed_config_keys,
        path="media provider library_login.managed_config_keys",
        allow_empty=True,
    )
    unknown_keys = sorted(set(managed_keys) - config_field_keys)
    if unknown_keys:
        raise ValueError(
            "media provider library_login.managed_config_keys 包含未知字段: "
            + ", ".join(unknown_keys)
        )
    if not isinstance(value.timeout_seconds, int) or isinstance(value.timeout_seconds, bool):
        raise TypeError("media provider library_login.timeout_seconds 必须是整数")
    if not 30 <= value.timeout_seconds <= 900:
        raise ValueError("media provider library_login.timeout_seconds 超出范围")


def validate_media_provider_extension(
    *,
    plugin_id: str,
    extension: PluginExtension,
) -> MediaProviderBundle:
    """Validate a bundle declaration without constructing any provider.

    ``build_storage``/``prepare_library`` and optional download methods are
    deliberately never called here.  They may perform network or filesystem
    work and are only invoked after a library/client is configured.
    """
    if extension.key != MEDIA_PROVIDER_EXTENSION_KEY:
        raise ValueError(f"扩展点 key 不匹配: {extension.key}")
    bundle = extension.data
    provider_key = getattr(bundle, "provider_key", None)
    if not isinstance(provider_key, str) or not provider_key or provider_key.strip() != provider_key:
        raise ValueError("media provider provider_key 必须是非空字符串")
    display_name = getattr(bundle, "display_name", None)
    if not isinstance(display_name, str) or not display_name or display_name.strip() != display_name:
        raise ValueError("media provider display_name 必须是非空字符串")
    _validate_i18n_map(
        getattr(bundle, "display_name_i18n", None),
        path="media provider display_name_i18n",
    )

    fields = _validate_config_fields(
        getattr(bundle, "library_config_fields", None),
        owner="media provider",
    )
    _validate_playback_deliveries(getattr(bundle, "playback_deliveries", None))
    _validate_merged_playback_format(getattr(bundle, "merged_playback_format", None))
    library_login = getattr(bundle, "library_login", None)
    _validate_library_login(
        library_login,
        config_field_keys={field.key for field in fields},
    )
    if library_login is not None and not callable(
        getattr(bundle, "resolve_library_login", None)
    ):
        raise TypeError("media provider bundle 缺少必需操作: resolve_library_login")
    _require_methods(
        bundle,
        ("prepare_library", "build_storage"),
        owner="media provider bundle",
    )

    missing = object()
    downloads = getattr(bundle, "downloads", missing)
    if downloads is missing:
        raise TypeError("media provider bundle 缺少 downloads 字段")
    if downloads is not None:
        # Protocols are intentionally not runtime-checkable; validate the
        # structural contract without invoking plugin code.
        _require_methods(
            downloads,
            ("prepare_client", "test_client", "build"),
            owner="media provider downloads",
        )
        _validate_config_fields(
            getattr(downloads, "config_fields", None),
            owner="media provider downloads",
        )
    return bundle


__all__ = [
    "MEDIA_PROVIDER_EXTENSION_KEY",
    "validate_media_provider_extension",
]
