"""Validator for the single ``media.provider`` bundle extension."""

from __future__ import annotations

from collections.abc import Iterable

from src.plugins.contracts import PluginExtension
from src.plugins.provider_protocol import (
    MEDIA_PROVIDER_EXTENSION_KEY,
    ConfigField,
    MediaProviderBundle,
)


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

    _validate_config_fields(
        getattr(bundle, "library_config_fields", None),
        owner="media provider",
    )
    _validate_playback_deliveries(getattr(bundle, "playback_deliveries", None))
    _validate_merged_playback_format(getattr(bundle, "merged_playback_format", None))
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
