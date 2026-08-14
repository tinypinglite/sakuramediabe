"""Movie 字段主权唯一写入网关（v2-lite）。

受保护字段（PROTECTED_MOVIE_FIELDS 白名单）只允许经本类写入，全部走单条条件
UPDATE，靠 PostgreSQL 行级锁 + 条件求值（READ COMMITTED 下并发行更新会在
EvalPlanQual 阶段重新评估 WHERE）保证原子性：

- ``patch_plugin``：插件写字段并取得/续持 owner；要求 revision 匹配，任一条件
  失败整次零修改，无 TOCTOU 窗口。
- ``update_host_unowned``：宿主只更新尚未被接管的字段；NULL-safe 变化检测，
  任一字段真正变化才递增 revision 并刷新 updated_at。

字段名全部来自宿主白名单常量，不接收任意字符串；值一律参数化。
"""

from __future__ import annotations

import json
from typing import Any

from src.model.catalog.movies import MOVIE_FIELD_CODECS, PROTECTED_MOVIE_FIELDS, Movie


class MovieOwnershipGateway:
    """受保护字段唯一写入入口；宿主窄更新与插件 patch 都不得绕过本类。"""

    @staticmethod
    def _owner_key(plugin_id: str) -> str:
        return f"plugin:{plugin_id}"

    @staticmethod
    def _validate_fields(fields: dict[str, Any], *, allow_none: bool = False) -> None:
        if not fields:
            raise ValueError("fields 不能为空")
        invalid = set(fields) - set(PROTECTED_MOVIE_FIELDS)
        if invalid:
            raise ValueError(f"非受保护字段: {sorted(invalid)}")
        # 字段 codec：值类型必须与白名单声明的 codec 一致，防止非法类型直接落库。
        for name, value in fields.items():
            expected_type = MOVIE_FIELD_CODECS.get(name)
            if expected_type is None:
                continue
            if value is None:
                # 宿主写路径（update_host_unowned）放行 None：maker_name / director_name /
                # summary 等列允许 NULL，远端详情缺失时以 None 落库是合法数据；
                # 插件 patch 路径保持 str-only（allow_none=False），拒绝 None。
                if not allow_none:
                    raise ValueError(f"字段 {name} 值类型错误: 期望 {expected_type.__name__}")
                continue
            if not isinstance(value, expected_type):
                raise ValueError(f"字段 {name} 值类型错误: 期望 {expected_type.__name__}")

    @classmethod
    def patch_plugin(
        cls,
        movie_id: int,
        plugin_id: str,
        fields: dict[str, Any],
        expected_revision: int,
    ) -> bool:
        """插件写字段：每个字段要求“未接管或 owner 是当前插件”，且 revision 匹配。

        单条 UPDATE 原子完成：任一字段条件失败则整次零修改并返回 False，插件应
        重新读取 snapshot 后决定是否重试。成功时字段值、owner、revision、updated_at
        在同一语句中提交。
        """
        cls._validate_fields(fields)
        owner = cls._owner_key(plugin_id)
        table = Movie._meta.table_name
        assignments = ", ".join(f"{name} = %s" for name in fields)
        owner_conditions = " AND ".join(
            "(field_owners->>%s IS NULL OR field_owners->>%s = %s)"
            for _ in fields
        )
        owner_payload = json.dumps(
            {name: owner for name in fields}, ensure_ascii=False
        )
        params: list[Any] = [*fields.values(), owner_payload, movie_id, expected_revision]
        for name in fields:
            params.extend((name, name, owner))
        cursor = Movie._meta.database.execute_sql(
            f"""
            UPDATE {table}
            SET {assignments},
                field_owners = field_owners || %s::jsonb,
                mutation_revision = mutation_revision + 1,
                updated_at = now()
            WHERE id = %s
              AND mutation_revision = %s
              AND {owner_conditions}
            """,
            params,
        )
        return cursor.rowcount == 1

    @classmethod
    def update_host_unowned(cls, movie_id: int, fields: dict[str, Any]) -> int:
        """宿主只更新尚未被接管的受保护字段（单条 UPDATE，字段级独立判断）。

        每个字段仅在当前没有 owner 时改写（owner 条件放 SET 的 CASE 内，避免一个
        字段被接管导致整条跳过）；NULL-safe（IS DISTINCT FROM）变化检测，任一字段
        “未接管且值真正变化”才递增 revision 并刷新 updated_at。返回受影响行数。
        """
        cls._validate_fields(fields, allow_none=True)
        table = Movie._meta.table_name
        # SET 按字段 CASE：未接管写入新值，已接管保留 DB 现值（插件值）。
        assignments = ", ".join(
            f"{name} = CASE WHEN field_owners->>%s IS NULL THEN %s ELSE {name} END"
            for name in fields
        )
        revision_terms = " + ".join(
            f"(CASE WHEN field_owners->>%s IS NULL AND {name} IS DISTINCT FROM %s"
            f" THEN 1 ELSE 0 END)"
            for name in fields
        )
        changed_terms = " OR ".join(
            f"(field_owners->>%s IS NULL AND {name} IS DISTINCT FROM %s)"
            for name in fields
        )
        params: list[Any] = []
        for name, value in fields.items():
            params.extend((name, value))
        for name, value in fields.items():
            params.extend((name, value))
        for name, value in fields.items():
            params.extend((name, value))
        params.append(movie_id)
        cursor = Movie._meta.database.execute_sql(
            f"""
            UPDATE {table}
            SET {assignments},
                mutation_revision = mutation_revision + ({revision_terms}),
                updated_at = CASE WHEN {changed_terms} THEN now() ELSE updated_at END
            WHERE id = %s
            """,
            params,
        )
        return cursor.rowcount

    @classmethod
    def release_plugin_owners(
        cls,
        plugin_id: str,
        fields: tuple[str, ...] | None = None,
    ) -> int:
        """管理员解除插件对字段的接管（清理端点，CLI 调用）。

        ``fields`` 为 None 时清除该插件全部 owner 记录；指定时仅清除列出的字段
        （必须属于白名单）。只动 field_owners 映射，不改字段值与 revision。
        返回受影响行数。
        """
        owner = cls._owner_key(plugin_id)
        table = Movie._meta.table_name
        if fields is not None:
            invalid = set(fields) - set(PROTECTED_MOVIE_FIELDS)
            if invalid:
                raise ValueError(f"非受保护字段: {sorted(invalid)}")
            if not fields:
                raise ValueError("fields 不能为空")
            # 按 owner 过滤重建映射，只摘除属于目标插件的 key：jsonb_each_text 逐
            # key 判断，不会误摘其他插件接管的字段（不能用连续减法：jsonb - NULL
            # 左结合会把整条结果污染成 NULL）。
            key_placeholders = ", ".join("%s" for _ in fields)
            # 任一目标字段属于该插件即整行更新（字段级独立摘除）。
            conditions = " OR ".join(
                "field_owners->>%s = %s" for _ in fields
            )
            params: list[Any] = [owner, *fields]
            for name in fields:
                params.extend((name, owner))
            cursor = Movie._meta.database.execute_sql(
                f"""
                UPDATE {table}
                SET field_owners = (
                    SELECT COALESCE(jsonb_object_agg(k.key, k.value), '{{}}'::jsonb)
                    FROM jsonb_each_text(field_owners) AS k
                    WHERE NOT (k.value = %s AND k.key IN ({key_placeholders}))
                )
                WHERE {conditions}
                """,
                params,
            )
            return cursor.rowcount
        # 清除该插件全部 owner：按 owner 值过滤重建映射。
        cursor = Movie._meta.database.execute_sql(
            f"""
            UPDATE {table}
            SET field_owners = COALESCE(
                (SELECT jsonb_object_agg(key, value) FROM jsonb_each_text(field_owners)
                 WHERE value <> %s),
                '{{}}'::jsonb
            )
            WHERE EXISTS (
                SELECT 1 FROM jsonb_each_text(field_owners) WHERE value = %s
            )
            """,
            (owner, owner),
        )
        return cursor.rowcount
