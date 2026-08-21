import json
from typing import Any

from pydantic import BaseModel, ValidationError

from src.api.exception.errors import ApiError
from src.config.config import (
    Settings,
    load_persisted_settings,
    persist_settings,
    settings,
    settings_write_lock,
)
from src.schema.system.config import (
    ConfigResource,
    ConfigUpdateResource,
)

# 本 API 不接管的顶层键（节名或顶层字段名统一收敛在一起）：
# - "auth" 节整体交由专用接口管理：
#   - auth.username / auth.password 由 /account 管理（运行时账号在 DB User 表，改配置无效）
#   - auth.secret_key / auth.file_signature_secret 由 ensure_runtime_config() 首启自举，
#     运行时若被改，会连带作废所有 access token 与已发签名 URL，不该经通用配置接口暴露
#   - auth.algorithm / *_expire_minutes 边缘价值低，需要时手动改 toml + 重启
# - "enable_docs" 顶层字段仍保留在 Settings（默认 False = 关闭 Swagger/ReDoc），
#   开发/排障时可通过 toml 或环境变量手动打开并重启，不通过接口暴露/修改。
# - "plugins" 节包含可信代码启用清单及插件私有配置：
#   API / APS 都在 import 阶段读取，且可能包含凭据，只允许手工改 toml 后重启整个服务。
READONLY_KEYS: frozenset[str] = frozenset({"auth", "enable_docs", "plugins"})

def _is_config_section(annotation: Any) -> bool:
    # 顶层字段是否为一个配置子节（Pydantic BaseModel 子类），用于区分子节 dict 与顶层标量。
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)

class ConfigService:
    @staticmethod
    def _json_safe_values(config: Settings = settings) -> dict[str, Any]:
        # 走 model_dump_json 往返，保证 set->list、enum->value、datetime->str 全部 json 安全，
        # 并与落盘序列化（_build_persistable_settings）保持一致。
        return json.loads(config.model_dump_json())

    @classmethod
    def _public_values(cls, config: Settings = settings) -> dict[str, Any]:
        # 剔除只读节，避免 auth/plugin 私密配置外流或启动期开关混入前端可改集合。
        values = json.loads(config.model_dump_json())
        for key in READONLY_KEYS:
            values.pop(key, None)
        return values

    @classmethod
    def get_config(cls) -> ConfigResource:
        return ConfigResource(values=cls._public_values())

    @classmethod
    def update_config(cls, patch: dict[str, Any]) -> ConfigUpdateResource:
        if not patch:
            raise ApiError(
                422,
                "empty_config_update",
                "At least one field must be provided",
            )

        cls._reject_unknown_fields(patch)

        with settings_write_lock():
            # 每次从当前磁盘快照合并，连续局部 PATCH 不会回写旧进程快照覆盖前次结果。
            merged = cls._json_safe_values(load_persisted_settings())
            for key, value in patch.items():
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged_section = dict(merged[key])
                    merged_section.update(value)
                    merged[key] = merged_section
                else:
                    merged[key] = value

            try:
                # 先按节直接严格校验：pydantic-settings 的 BaseSettings.model_validate 不会把 context
                # 透传到子模型 validator，所以严格档必须在每个子模型上单独触发；触发一次即可让
                # URL/proxy/cron 校验器进入 strict 分支并抛错，阻止把非法值写入磁盘。
                cls._strict_validate_sections(merged)
                # 严格校验通过后，顶层组装走 BaseSettings 常规路径（宽松档，值已被上面严格过）。
                new_settings = Settings.model_validate(merged)
            except ValidationError as exc:
                # 类型/语义（cron、url）校验失败统一收敛为可控错误码，不外泄 pydantic 原始 500。
                raise ApiError(
                    422,
                    "invalid_config_value",
                    "Configuration validation failed",
                    {"errors": exc.errors(include_url=False, include_context=False)},
                ) from exc

            # 普通配置只原子写盘；当前 API/APS 进程继续使用启动时快照，重启后统一生效。
            persist_settings(new_settings)
        return ConfigUpdateResource(
            values=cls._public_values(new_settings),
            restart_required=["api", "aps"],
        )

    @staticmethod
    def _strict_validate_sections(merged: dict[str, Any]) -> None:
        # 遍历 merged 每个已知子节，直接调用子模型的 model_validate 并传 strict context，
        # 让 field/model validator 走严格分档。子模型是纯 BaseModel（非 BaseSettings），
        # 顶层传入的 context 会正常透传给它自己的 validator。
        for key, value in merged.items():
            if not isinstance(value, dict):
                continue
            field = Settings.model_fields.get(key)
            if field is None:
                continue
            annotation = field.annotation
            if _is_config_section(annotation):
                annotation.model_validate(value, context={"strict": True})

    @staticmethod
    def _reject_unknown_fields(patch: dict[str, Any]) -> None:
        # 显式白名单校验，拼错/不存在的配置 key 直接 422，而非静默丢弃（配置场景更安全）。
        section_fields = Settings.model_fields
        for key, value in patch.items():
            # 只读键显式拒绝并指向替代路径，
            # 避免与 unknown_config_field 语义混淆。
            if key in READONLY_KEYS:
                raise ApiError(
                    422,
                    "readonly_config_key",
                    f"Config key '{key}' is not modifiable via this API",
                    {"field": key},
                )
            if key not in section_fields:
                raise ApiError(
                    422,
                    "unknown_config_field",
                    f"Unknown config field: {key}",
                    {"field": key},
                )
            annotation = section_fields[key].annotation
            if _is_config_section(annotation):
                if not isinstance(value, dict):
                    raise ApiError(
                        422,
                        "invalid_config_value",
                        f"Config section '{key}' must be an object",
                        {"field": key},
                    )
                for sub_key in value:
                    if sub_key not in annotation.model_fields:
                        raise ApiError(
                            422,
                            "unknown_config_field",
                            f"Unknown config field: {key}.{sub_key}",
                            {"field": f"{key}.{sub_key}"},
                        )
