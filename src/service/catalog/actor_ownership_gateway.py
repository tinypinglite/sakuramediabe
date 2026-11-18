"""演员资料的受控写入；身份、头像和订阅不在插件写入白名单内。"""

import json
from datetime import date
from typing import Any

from src.model.catalog.actors import (
    ACTOR_FIELD_ALLOWED_VALUES,
    ACTOR_FIELD_CODECS,
    ACTOR_OWNER_TRACKED_FIELDS,
    PROTECTED_ACTOR_FIELDS,
    Actor,
)

HOST_METADATA_FIELD_OWNER = "host:metadata"
MANUAL_ACTOR_FIELD_OWNER = "host:manual"


class ActorOwnershipGateway:
    @staticmethod
    def _normalize_fields(fields: dict[str, Any]) -> dict[str, Any]:
        if not fields or set(fields) - PROTECTED_ACTOR_FIELDS:
            raise ValueError("fields 必须是非空的演员资料字段集合")
        normalized = dict(fields)
        for name, value in fields.items():
            if value is None:
                if name in ACTOR_FIELD_ALLOWED_VALUES:
                    raise ValueError(f"字段 {name} 值必须是 {sorted(ACTOR_FIELD_ALLOWED_VALUES[name])}")
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
            allowed_values = ACTOR_FIELD_ALLOWED_VALUES.get(name)
            if allowed_values is not None and value not in allowed_values:
                raise ValueError(f"字段 {name} 值必须是 {sorted(allowed_values)}")
            if allowed_values is None and expected is int and not 1 <= value <= 2147483647:
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
    def update_host_source(
        cls,
        actor_id: int,
        fields: dict[str, Any],
        *,
        owner: str = HOST_METADATA_FIELD_OWNER,
    ) -> bool:
        """宿主权威来源更新字段；可替换插件 owner，但不覆盖人工 owner。"""
        fields = cls._normalize_fields(fields)
        if not isinstance(owner, str) or not owner.startswith("host:") or owner == MANUAL_ACTOR_FIELD_OWNER:
            raise ValueError("owner 必须是非人工的宿主来源 owner")
        assignments = ", ".join(f"{name} = %s" for name in fields)
        owner_conditions = " AND ".join(
            "(field_owners->>%s IS NULL OR field_owners->>%s <> %s)" for _ in fields
        )
        changed_conditions = " OR ".join(
            f"({name} IS DISTINCT FROM %s OR field_owners->>%s IS DISTINCT FROM %s)"
            for name in fields
        )
        params: list[Any] = [
            *fields.values(),
            json.dumps({name: owner for name in fields}),
            actor_id,
        ]
        for name in fields:
            params.extend((name, name, MANUAL_ACTOR_FIELD_OWNER))
        for name, value in fields.items():
            params.extend((value, name, owner))
        cursor = Actor._meta.database.execute_sql(
            f"""
            UPDATE actor SET {assignments},
                field_owners = field_owners || %s::jsonb,
                mutation_revision = mutation_revision + 1, updated_at = now()
            WHERE id = %s AND {owner_conditions} AND ({changed_conditions})
            """,
            params,
        )
        return cursor.rowcount == 1

    @classmethod
    def release_plugin_owners(cls, plugin_id: str, fields: tuple[str, ...] | None = None) -> int:
        """管理员释放归属，保留资料；推进版本使释放前的快照失效。"""
        if fields is not None and (not fields or set(fields) - ACTOR_OWNER_TRACKED_FIELDS):
            raise ValueError("fields 必须是非空的演员资料或头像字段集合")
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

    @classmethod
    def _write_profile_image_override(
        cls,
        actor_id: int,
        *,
        image_id: int | None,
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """头像覆盖列与 ``field_owners`` 键的统一写入。

        ``image_id`` 有值即写覆盖并接管 owner，为空即清覆盖并摘除 owner 键。
        给 ``expected_revision`` 时是插件路径：要求版本一致，且写路径只接管"无主
        空槽"（无主旧覆盖行按人工处理）或自己持有的槽位，清路径只清自己持有的；
        ``owner`` 也只在该路径参与条件。不给版本时是人工路径，可覆盖任何 owner。
        """
        if expected_revision is not None and (
            type(expected_revision) is not int or expected_revision < 0
        ):
            raise ValueError("expected_revision 必须是非负整数")
        if image_id is not None:
            assignments = (
                "profile_image_override_id = %s, "
                "field_owners = field_owners || %s::jsonb"
            )
            params: list[Any] = [
                image_id,
                json.dumps({"profile_image_override": owner}, ensure_ascii=False),
            ]
        else:
            assignments = (
                "profile_image_override_id = NULL, "
                "field_owners = field_owners - 'profile_image_override'"
            )
            params = []
        params.append(actor_id)
        conditions = ""
        if expected_revision is not None:
            conditions = " AND mutation_revision = %s"
            params.append(expected_revision)
            if image_id is not None:
                conditions += (
                    " AND ((profile_image_override_id IS NULL"
                    " AND field_owners->>'profile_image_override' IS NULL)"
                    " OR field_owners->>'profile_image_override' = %s)"
                )
            else:
                conditions += " AND field_owners->>'profile_image_override' = %s"
            params.append(owner)
        cursor = Actor._meta.database.execute_sql(
            f"""
            UPDATE actor
            SET {assignments},
                mutation_revision = mutation_revision + 1,
                updated_at = now()
            WHERE id = %s{conditions}
            """,
            params,
        )
        return cursor.rowcount == 1

    @classmethod
    def replace_profile_image_override(
        cls,
        actor_id: int,
        plugin_id: str,
        image_id: int,
        expected_revision: int,
    ) -> bool:
        """插件写头像覆盖：仅当当前没有覆盖（或无主旧覆盖被人工接管）或 owner 是自己。"""
        return cls._write_profile_image_override(
            actor_id,
            image_id=image_id,
            owner=f"plugin:{plugin_id}",
            expected_revision=expected_revision,
        )

    @classmethod
    def clear_profile_image_override(
        cls,
        actor_id: int,
        plugin_id: str,
        expected_revision: int,
    ) -> bool:
        """插件移除自己的头像覆盖；人工覆盖不受影响。"""
        return cls._write_profile_image_override(
            actor_id,
            image_id=None,
            owner=f"plugin:{plugin_id}",
            expected_revision=expected_revision,
        )

    @classmethod
    def set_manual_profile_image_override(cls, actor_id: int, image_id: int) -> bool:
        """人工上传头像：可覆盖任何插件 owner，并标记为人工持有。"""
        return cls._write_profile_image_override(
            actor_id, image_id=image_id, owner=MANUAL_ACTOR_FIELD_OWNER
        )

    @classmethod
    def clear_manual_profile_image_override(cls, actor_id: int) -> bool:
        """人工清除头像覆盖：同时清掉 owner 记录。"""
        return cls._write_profile_image_override(actor_id, image_id=None)
