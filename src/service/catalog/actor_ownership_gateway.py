"""演员资料的受控写入；身份、头像和订阅不在插件写入白名单内。"""

import json
from datetime import date
from typing import Any

from src.model.catalog.actors import ACTOR_FIELD_CODECS, PROTECTED_ACTOR_FIELDS, Actor


class ActorOwnershipGateway:
    @staticmethod
    def _normalize_fields(fields: dict[str, Any]) -> dict[str, Any]:
        if not fields or set(fields) - PROTECTED_ACTOR_FIELDS:
            raise ValueError("fields 必须是非空的演员资料字段集合")
        normalized = dict(fields)
        for name, value in fields.items():
            if value is None:
                continue
            expected = ACTOR_FIELD_CODECS[name]
            if expected is date and isinstance(value, str):
                try:
                    value = date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("birthday 必须是 YYYY-MM-DD 日期") from exc
                if value.isoformat() != fields[name]:
                    raise ValueError("birthday 必须是 YYYY-MM-DD 日期")
                normalized[name] = value
            if type(value) is not expected:
                raise ValueError(f"字段 {name} 值类型错误: 期望 {expected.__name__}")
            if expected is int and not 1 <= value <= 2147483647:
                raise ValueError(f"字段 {name} 必须是正整数厘米值")
            if expected is str and (not value.strip() or len(value) > 255):
                raise ValueError(f"字段 {name} 必须是 1 到 255 字符的非空文本；清空请传 None")
        return normalized

    @classmethod
    def patch_plugin(
        cls, actor_id: int, plugin_id: str, fields: dict[str, Any], expected_revision: int,
    ) -> bool:
        """任一字段归属或版本不匹配时整次零修改；None 显式清空并保留归属。"""
        fields = cls._normalize_fields(fields)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision 必须是非负整数")
        owner = f"plugin:{plugin_id}"
        assignments = ", ".join(f"{name} = %s" for name in fields)
        conditions = " AND ".join(
            "(field_owners->>%s IS NULL OR field_owners->>%s = %s)" for _ in fields
        )
        params = [
            *fields.values(), json.dumps({name: owner for name in fields}),
            actor_id, expected_revision,
        ]
        for name in fields:
            params.extend((name, name, owner))
        cursor = Actor._meta.database.execute_sql(
            f"""
            UPDATE actor SET {assignments},
                field_owners = field_owners || %s::jsonb,
                mutation_revision = mutation_revision + 1, updated_at = now()
            WHERE id = %s AND mutation_revision = %s AND {conditions}
            """,
            params,
        )
        return cursor.rowcount == 1

    @classmethod
    def release_plugin_owners(cls, plugin_id: str, fields: tuple[str, ...] | None = None) -> int:
        """管理员释放归属，保留资料；推进版本使释放前的快照失效。"""
        if fields is not None and (not fields or set(fields) - PROTECTED_ACTOR_FIELDS):
            raise ValueError("fields 必须是非空的演员资料字段集合")
        condition = "value = %s"
        params: list[Any] = [f"plugin:{plugin_id}"]
        if fields is not None:
            condition += f" AND key IN ({', '.join('%s' for _ in fields)})"
            params.extend(fields)
        cursor = Actor._meta.database.execute_sql(
            f"""
            UPDATE actor SET field_owners = (
                SELECT COALESCE(jsonb_object_agg(key, value), '{{}}'::jsonb)
                FROM jsonb_each_text(field_owners) WHERE NOT ({condition})
            ), mutation_revision = mutation_revision + 1, updated_at = now()
            WHERE EXISTS (
                SELECT 1 FROM jsonb_each_text(field_owners) WHERE {condition}
            )
            """,
            [*params, *params],
        )
        return cursor.rowcount
