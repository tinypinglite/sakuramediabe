"""插件表单默认值：不校验已保存配置，也不要求必填字段已有值。"""

from typing import Any

from pydantic import BaseModel, TypeAdapter


def settings_defaults(model: type[BaseModel]) -> dict[str, Any]:
    return _model_defaults(model, set())


def _model_defaults(model: type[BaseModel], parents: set[type[BaseModel]]) -> dict[str, Any]:
    if model in parents:
        return {}
    defaults = {}
    for name, field in model.model_fields.items():
        key = field.alias or name
        if field.is_required():
            annotation = field.annotation
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                defaults[key] = _model_defaults(annotation, parents | {model})
        else:
            if field.default_factory_takes_validated_data:
                raise ValueError(f"配置默认值不能依赖其他字段: {key}")
            value = field.get_default(call_default_factory=True)
            defaults[key] = TypeAdapter(field.annotation).dump_python(value, mode="json", by_alias=True)
    return defaults
