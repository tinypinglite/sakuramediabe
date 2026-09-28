"""演员合并：来源记录归并到保留记录后，来源退化为墓碑指针。

只允许人工显式触发；合并后所有查询/写入通过 ``Actor.resolve_canonical`` 收敛到保留记录。
"""

from __future__ import annotations

import json
from typing import Any

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import Actor, MovieActor, get_database
from src.model.catalog.actors import (
    PROTECTED_ACTOR_FIELDS,
    merge_actor_alias_name,
    split_actor_alias_name,
)
from src.schema.catalog.actors import ActorDetailResource
from src.service.catalog.actor_ownership_gateway import MANUAL_ACTOR_FIELD_OWNER
from src.service.catalog.actor_service import ActorService


class ActorMergeService:
    @classmethod
    def merge_actors(
        cls, target_actor_id: int, source_actor_ids: list[int]
    ) -> ActorDetailResource:
        source_ids = list(dict.fromkeys(source_actor_ids))
        with get_database().atomic():
            target = cls._locked_actor(target_actor_id)
            if target.merged_into_id is not None:
                target = cls._locked_actor(target.merged_into_id)

            active_sources: list[Actor] = []
            for source_id in source_ids:
                source = cls._locked_actor(source_id)
                if source.id == target.id:
                    raise ApiError(
                        422,
                        "invalid_actor_merge",
                        "不能把演员合并到自身",
                        {"reason": "merge_self", "actor_id": source.id},
                    )
                if source.merged_into_id is None:
                    active_sources.append(source)
                    continue
                if source.merged_into_id == target.id:
                    continue
                raise ApiError(
                    422,
                    "invalid_actor_merge",
                    "来源演员已合并到其他演员",
                    {"reason": "source_already_merged", "actor_id": source.id},
                )

            if active_sources:
                cls._apply_merge(target, active_sources)

        return ActorService.get_actor_detail(target.id)

    @staticmethod
    def _locked_actor(actor_id: int) -> Actor:
        actor = (
            Actor.select().where(Actor.id == actor_id).for_update().get_or_none()
        )
        if actor is None:
            raise ApiError(
                404, "actor_not_found", "演员不存在", {"actor_id": actor_id}
            )
        return actor

    @staticmethod
    def _field_is_empty(actor: Actor, field: str) -> bool:
        value = getattr(actor, field)
        if field == "gender":
            return value in (None, 0)
        return value is None

    @classmethod
    def _apply_merge(cls, target: Actor, sources: list[Actor]) -> None:
        database = get_database()
        source_ids = [source.id for source in sources]
        placeholders = ", ".join(["%s"] * len(source_ids))

        # 关联搬运：同一部影片两边都有时按 (movie, actor) 唯一约束去重。
        database.execute_sql(
            f"""
            INSERT INTO movie_actor (movie_id, actor_id)
            SELECT movie_id, %s FROM movie_actor WHERE actor_id IN ({placeholders})
            ON CONFLICT (movie_id, actor_id) DO NOTHING
            """,
            [target.id, *source_ids],
        )
        MovieActor.delete().where(MovieActor.actor.in_(source_ids)).execute()

        alias_names: list[str] = []
        for source in sources:
            alias_names.append(source.name)
            alias_names.extend(split_actor_alias_name(source.alias_name))
            override = (source.display_name_override or "").strip()
            if override:
                alias_names.append(override)
        merged_alias_name = merge_actor_alias_name(
            target.name, alias_names, target.alias_name
        )

        source_subscribed = [source for source in sources if source.is_subscribed]
        is_subscribed = target.is_subscribed or bool(source_subscribed)
        subscribed_at = target.subscribed_at
        subscribed_dates = [
            actor.subscribed_at
            for actor in (target, *source_subscribed)
            if actor.subscribed_at is not None
        ]
        if is_subscribed and subscribed_dates:
            earliest = min(subscribed_dates)
            if subscribed_at is None or earliest < subscribed_at:
                subscribed_at = earliest
        elif is_subscribed and subscribed_at is None:
            subscribed_at = utc_now_for_db()

        field_updates: dict[str, Any] = {}
        owner_updates: dict[str, str] = {}
        for field in sorted(PROTECTED_ACTOR_FIELDS):
            if not cls._field_is_empty(target, field):
                continue
            for source in sources:
                if cls._field_is_empty(source, field):
                    continue
                owners = source.field_owners or {}
                if owners.get(field) == MANUAL_ACTOR_FIELD_OWNER:
                    continue
                field_updates[field] = getattr(source, field)
                if owners.get(field):
                    owner_updates[field] = owners[field]
                break

        profile_image_id = target.profile_image_id
        profile_image_override_id = target.profile_image_override_id
        moved_image: tuple[int, str] | None = None
        if profile_image_id is None and profile_image_override_id is None:
            for source in sources:
                if source.profile_image_override_id is not None:
                    profile_image_override_id = source.profile_image_override_id
                    moved_image = (source.id, "profile_image_override_id")
                    break
            if moved_image is None:
                for source in sources:
                    if source.profile_image_id is not None:
                        profile_image_id = source.profile_image_id
                        moved_image = (source.id, "profile_image_id")
                        break

        assignments = [
            "alias_name = %s",
            "is_subscribed = %s",
            "subscribed_at = %s",
        ]
        params: list[Any] = [merged_alias_name, is_subscribed, subscribed_at]
        if is_subscribed:
            # 同步任务会覆盖墓碑的 javdb_id，强制下一次全量以补齐来源 ID 的历史影片。
            assignments.append("subscribed_movies_full_synced_at = NULL")
        for field, value in field_updates.items():
            assignments.append(f"{field} = %s")
            params.append(value)
        if owner_updates:
            assignments.append("field_owners = field_owners || %s::jsonb")
            params.append(json.dumps(owner_updates, ensure_ascii=False))
        if field_updates:
            assignments.append("mutation_revision = mutation_revision + 1")
        if profile_image_id != target.profile_image_id:
            assignments.append("profile_image_id = %s")
            params.append(profile_image_id)
        if profile_image_override_id != target.profile_image_override_id:
            assignments.append("profile_image_override_id = %s")
            params.append(profile_image_override_id)
        assignments.append("updated_at = now()")
        params.append(target.id)
        database.execute_sql(
            f"UPDATE actor SET {', '.join(assignments)} WHERE id = %s",
            params,
        )

        database.execute_sql(
            f"""
            UPDATE actor SET is_subscribed = FALSE,
                subscribed_at = NULL,
                merged_into_id = %s,
                updated_at = now()
            WHERE id IN ({placeholders})
            """,
            [target.id, *source_ids],
        )
        database.execute_sql(
            f"""
            UPDATE actor SET merged_into_id = %s, updated_at = now()
            WHERE merged_into_id IN ({placeholders})
            """,
            [target.id, *source_ids],
        )
        if moved_image is not None:
            moved_source_id, moved_field = moved_image
            database.execute_sql(
                f"UPDATE actor SET {moved_field} = NULL, updated_at = now() WHERE id = %s",
                [moved_source_id],
            )
