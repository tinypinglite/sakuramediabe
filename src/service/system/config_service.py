import json
from typing import Any

from pydantic import BaseModel, ValidationError

from src.api.exception.errors import ApiError
from src.config.config import (
    Settings,
    settings,
)
from src.config.config import (
    update_settings as persist_settings,
)
from src.schema.system.config import (
    ConfigEffectLevel,
    ConfigResource,
    ConfigUpdateResource,
    PendingRestartField,
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

# 节级默认生效方式。依据双进程部署（api / aps 各持一份 settings 快照）与各配置的读取时机：
# refresh_runtime_settings 只刷新 api 进程内存，故被定时任务消费或启动期读取一次的配置无法即时生效。
SECTION_EFFECT: dict[str, ConfigEffectLevel] = {
    "database": ConfigEffectLevel.RESTART_API,          # 连接池启动建一次，不重连
    "media": ConfigEffectLevel.HOT,                      # 使用时现读
    "movie_info_translation": ConfigEffectLevel.HOT,     # 每次构造 client 现读
    "metadata": ConfigEffectLevel.HOT,                   # provider 每次 build 现读
    "scheduler": ConfigEffectLevel.RESTART_SCHEDULER,    # cron 装配时烘进 CronTrigger
    "downloads": ConfigEffectLevel.RESTART_SCHEDULER,    # 阈值由 aps 定时清理任务消费
    "media_import": ConfigEffectLevel.HOT,               # 每次浏览/导入现读，api 驱动
    "logging": ConfigEffectLevel.RESTART_API,            # 仅 configure_logging() 启动期应用
    "image_search": ConfigEffectLevel.HOT,               # 现读 + refresh 清 lru 单例
    "qdrant": ConfigEffectLevel.HOT,                     # 现读 + refresh 清 lru 单例
}

# 字段级覆盖：下载配置分别由 API SSE 与 APS 消费，按真实读取时机细分。
FIELD_EFFECT_OVERRIDE: dict[str, ConfigEffectLevel] = {
    "downloads.cloud115_progress_poll_interval_seconds": ConfigEffectLevel.HOT,
    "downloads.progress_stream_poll_interval_seconds": ConfigEffectLevel.RESTART_API,
}


def _is_config_section(annotation: Any) -> bool:
    # 顶层字段是否为一个配置子节（Pydantic BaseModel 子类），用于区分子节 dict 与顶层标量。
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def resolve_effect(dotted_key: str) -> ConfigEffectLevel:
    if dotted_key in FIELD_EFFECT_OVERRIDE:
        return FIELD_EFFECT_OVERRIDE[dotted_key]
    section = dotted_key.split(".", 1)[0]
    # 缺省保守取 restart_api：新增配置节若漏登记，宁可提示重启也不误示为即时生效。
    return SECTION_EFFECT.get(section, ConfigEffectLevel.RESTART_API)


class ConfigService:
    @staticmethod
    def _json_safe_values() -> dict[str, Any]:
        # 走 model_dump_json 往返，保证 set->list、enum->value、datetime->str 全部 json 安全，
        # 并与落盘序列化（_build_persistable_settings）保持一致。
        return json.loads(settings.model_dump_json())

    @classmethod
    def _public_values(cls) -> dict[str, Any]:
        # 剔除只读节，避免 auth/plugin 私密配置外流或启动期开关混入前端可改集合。
        values = cls._json_safe_values()
        for key in READONLY_KEYS:
            values.pop(key, None)
        return values

    @classmethod
    def get_config(cls) -> ConfigResource:
        effects: dict[str, ConfigEffectLevel] = {
            section: SECTION_EFFECT.get(section, ConfigEffectLevel.RESTART_API)
            for section in Settings.model_fields
            if section not in READONLY_KEYS
        }
        effects.update(FIELD_EFFECT_OVERRIDE)
        return ConfigResource(values=cls._public_values(), effects=effects)

    @classmethod
    def update_config(cls, patch: dict[str, Any]) -> ConfigUpdateResource:
        if not patch:
            raise ApiError(
                422,
                "empty_config_update",
                "At least one field must be provided",
            )

        cls._reject_unknown_fields(patch)

        # 以当前完整配置为基准做深合并：子节浅合并字段、顶层标量直接覆盖。
        # 注意合并基准使用完整快照（含只读节），保证 model_validate 得到的 Settings 与磁盘现状一致，
        # patch 已在 _reject_unknown_fields 拦住只读键，不会污染这些字段。
        merged = cls._json_safe_values()
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

        # 一步到位：落盘 toml + 刷新 api 进程内存全局 settings 并清依赖缓存。
        persist_settings(new_settings)

        applied, pending_restart = cls._classify_touched_fields(patch)
        return ConfigUpdateResource(
            values=cls._public_values(),
            applied=applied,
            pending_restart=pending_restart,
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

    @staticmethod
    def _classify_touched_fields(
        patch: dict[str, Any],
    ) -> tuple[list[str], list[PendingRestartField]]:
        applied: list[str] = []
        pending_restart: list[PendingRestartField] = []

        def classify(dotted: str) -> None:
            effect = resolve_effect(dotted)
            if effect is ConfigEffectLevel.HOT:
                applied.append(dotted)
            elif effect is ConfigEffectLevel.RESTART_API:
                pending_restart.append(PendingRestartField(field=dotted, restart="api"))
            else:
                pending_restart.append(
                    PendingRestartField(field=dotted, restart="scheduler")
                )

        for key, value in patch.items():
            if isinstance(value, dict):
                for sub_key in value:
                    classify(f"{key}.{sub_key}")
            else:
                classify(key)

        return applied, pending_restart
