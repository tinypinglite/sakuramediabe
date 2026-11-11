"""演员目录 service。

负责演员列表、详情、关联影片标识/标签/年份查询，以及 GFriends 演员搜索与导入。
阅读入口建议从 ``list_actors``、``get_actor_movie_ids``、``search_gfriends`` 开始。
"""

import json
import operator
import shutil
import tempfile
from calendar import monthrange
from collections.abc import Sequence
from datetime import date
from functools import reduce
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from peewee import JOIN, Case, fn
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError

from src.api.exception.errors import ApiError
from src.common.media_paths import media_image_root_path, normalize_asset_dir_name
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    build_ordered_expressions,
    playable_exists_expression,
    require_by_id,
    resolve_sort_expression,
)
from src.common.text_search import split_search_terms
from src.metadata.gfriends import get_gfriends_directory
from src.model import Actor, Image, Movie, MovieActor, MovieTag, Tag, get_database
from src.model.catalog.actors import (
    merge_actor_alias_name,
    split_actor_alias_name,
)
from src.model.expressions import year_expression
from src.schema.catalog.actors import (
    ActorCreateRequest,
    ActorCupFilterOption,
    ActorDetailResource,
    ActorFilterOptionsResource,
    ActorFilterRangeResource,
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorResource,
    ActorUpdateRequest,
    GfriendsActorCandidateResource,
    GfriendsActorImportRequest,
    YearResource,
)
from src.schema.catalog.movies import TagListItemResource
from src.schema.common.pagination import PageResponse
from src.service.catalog.actor_ownership_gateway import (
    MANUAL_ACTOR_FIELD_OWNER,
    ActorOwnershipGateway,
)
from src.service.catalog.actor_resolution_service import ActorResolutionService
from src.service.catalog.image_cleanup_service import ImageCleanupService


class _ProfileImageWriteConflict(Exception):
    """头像覆盖的 owner 或 revision 冲突，事务整体回滚。"""


class ActorService:
    """聚合 Actor 查询和本地/GFriends 导入流程。"""

    ACTOR_PROFILE_IMAGE_MAX_BYTES = 10 * 1024 * 1024
    ACTOR_PROFILE_IMAGE_MAX_DIMENSION = 4096
    ACTOR_PROFILE_IMAGE_OUTPUT_DIMENSION = 1024
    ACTOR_PROFILE_IMAGE_CONTENT_TYPES = frozenset(
        {"image/jpeg", "image/jpg", "image/png", "image/webp"}
    )
    ACTOR_PROFILE_IMAGE_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
    ACTOR_PROFILE_EDITABLE_FIELDS = frozenset(
        {
            "display_name_override",
            "gender",
            "birthday",
            "height_cm",
            "bust_cm",
            "waist_cm",
            "hips_cm",
            "cup",
            "birthplace",
            "blood_type",
        }
    )
    # 演员已是本地实体，姓名/别名允许人工维护。
    LOCAL_ACTOR_IDENTITY_FIELDS = frozenset({"name", "alias_name"})

    FEMALE_GENDER = 1
    MALE_GENDER = 2
    ACTOR_LIST_NULLABLE_SORT_FIELDS = {
        "subscribed_at",
        "age",
        "height_cm",
        "bust_cm",
        "waist_cm",
        "hips_cm",
        "waist_hip_ratio",
        "cup",
    }

    @staticmethod
    def _movie_count_expression():
        """按 movie_actor 关联实时计算演员影片数量。"""
        return MovieActor.select(fn.COUNT(MovieActor.id)).where(
            MovieActor.actor == Actor.id
        )

    @staticmethod
    def _playable_movie_count_expression():
        """按 movie_actor 关联实时计算演员可播放影片数量。"""
        return (
            MovieActor.select(fn.COUNT(MovieActor.id))
            .join(Movie, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == Actor.id, playable_exists_expression())
        )

    @staticmethod
    def _has_playable_movie_expression():
        """演员是否存在可播放影片。"""
        return fn.EXISTS(
            MovieActor.select(MovieActor.id)
            .join(Movie, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == Actor.id, playable_exists_expression())
        )

    @staticmethod
    def _normalized_cup_expression():
        return fn.NULLIF(fn.UPPER(fn.BTRIM(Actor.cup)), "")

    @staticmethod
    def _waist_hip_ratio_expression():
        return Actor.waist_cm.cast("REAL") / fn.NULLIF(Actor.hips_cm, 0)

    @classmethod
    def _actor_list_sort_field_map(cls):
        return {
            "subscribed_at": Actor.subscribed_at,
            "name": Actor.name,
            "movie_count": cls._movie_count_expression(),
            "playable_movie_count": cls._playable_movie_count_expression(),
            "age": Actor.birthday,
            "height_cm": Actor.height_cm,
            "bust_cm": Actor.bust_cm,
            "waist_cm": Actor.waist_cm,
            "hips_cm": Actor.hips_cm,
            "waist_hip_ratio": cls._waist_hip_ratio_expression(),
            "cup": cls._normalized_cup_expression(),
        }

    @classmethod
    def _actor_search_score_expression(cls, search_terms: Sequence[str]):
        """演员检索相关度：完整姓名 0、姓名前缀 1、姓名包含 2、别名包含 3。"""
        term_scores = []
        for term in search_terms:
            term_scores.append(
                Case(
                    None,
                    [
                        (fn.UPPER(Actor.name) == term.upper(), 0),
                        (Actor.name.startswith(term), 1),
                        (Actor.name.contains(term), 2),
                        (Actor.alias_name.contains(term), 3),
                    ],
                )
            )
        return reduce(operator.add, term_scores)

    @classmethod
    def _build_actor_list_sort(
        cls, sort: str | None, search_terms: Sequence[str] = ()
    ) -> Sequence:
        """解析演员列表排序表达式，并补充稳定的 id 次级排序。"""
        if search_terms and (sort is None or not sort.strip()):
            return [cls._actor_search_score_expression(search_terms), Actor.id.asc()]

        def _age_order(_field_name: str, direction: str) -> list:
            inverse_direction = "desc" if direction == "asc" else "asc"
            return build_ordered_expressions(
                Actor.birthday,
                inverse_direction,
                nullable=True,
                tie_breaker=Actor.id,
            )

        return resolve_sort_expression(
            sort,
            cls._actor_list_sort_field_map(),
            error_code="invalid_actor_filter",
            nullable_fields=cls.ACTOR_LIST_NULLABLE_SORT_FIELDS,
            tie_breaker=Actor.id,
            default=[Actor.id.asc()],
            extra_sort_builders={"age": _age_order},
        )

    @staticmethod
    def actor_query():
        """演员基础查询统一补齐头像，避免调用方重复 join。"""
        movie_count_expression = ActorService._movie_count_expression().alias(
            "movie_count"
        )
        profile_image_override = Image.alias()
        return (
            Actor.select(Actor, Image, profile_image_override, movie_count_expression)
            .join(Image, JOIN.LEFT_OUTER, on=(Actor.profile_image == Image.id))
            .switch(Actor)
            .join(
                profile_image_override,
                JOIN.LEFT_OUTER,
                on=(Actor.profile_image_override == profile_image_override.id),
                attr="profile_image_override",
            )
        )

    @classmethod
    def _actor_scope_conditions(
        cls,
        gender: ActorListGender = ActorListGender.ALL,
        subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
    ) -> list:
        conditions = []
        if gender == ActorListGender.FEMALE:
            conditions.append(Actor.gender == cls.FEMALE_GENDER)
        elif gender == ActorListGender.MALE:
            conditions.append(Actor.gender == cls.MALE_GENDER)

        if subscription_status == ActorListSubscriptionStatus.SUBSCRIBED:
            conditions.append(Actor.is_subscribed == True)
        elif subscription_status == ActorListSubscriptionStatus.UNSUBSCRIBED:
            conditions.append(Actor.is_subscribed == False)
        return conditions

    @staticmethod
    def _age_for_birthday(birthday: date, today: date) -> int:
        return (
            today.year
            - birthday.year
            - ((today.month, today.day) < (birthday.month, birthday.day))
        )

    @staticmethod
    def _years_before(today: date, years: int) -> date:
        year = today.year - years
        return date(year, today.month, min(today.day, monthrange(year, today.month)[1]))

    @classmethod
    def _actor_search_conditions(cls, search_terms: Sequence[str]):
        """多词检索：词之间 AND，词内姓名/别名 OR。"""
        return reduce(
            operator.and_,
            (
                (Actor.name.contains(term)) | (Actor.alias_name.contains(term))
                for term in search_terms
            ),
        )

    @classmethod
    def _filtered_actors(
        cls,
        gender: ActorListGender = ActorListGender.ALL,
        subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
        age_min: int | None = None,
        age_max: int | None = None,
        height_min: int | None = None,
        height_max: int | None = None,
        cups: Sequence[str] | None = None,
        has_playable_movies: bool = False,
        search_terms: Sequence[str] = (),
    ):
        """演员列表筛选统一收口到这里，保证 count 和 items 逻辑一致。"""
        if age_min is not None and age_max is not None and age_min > age_max:
            raise ApiError(
                422,
                "invalid_actor_filter",
                "age_min 不能大于 age_max",
                {"age_min": age_min, "age_max": age_max},
            )
        if (
            height_min is not None
            and height_max is not None
            and height_min > height_max
        ):
            raise ApiError(
                422,
                "invalid_actor_filter",
                "height_min 不能大于 height_max",
                {"height_min": height_min, "height_max": height_max},
            )

        query = cls.actor_query()
        query = query.where(Actor.merged_into.is_null())
        scope_conditions = cls._actor_scope_conditions(gender, subscription_status)
        if scope_conditions:
            query = query.where(*scope_conditions)
        today = utc_now_for_db().date()
        if age_min is not None:
            query = query.where(Actor.birthday <= cls._years_before(today, age_min))
        if age_max is not None:
            query = query.where(Actor.birthday > cls._years_before(today, age_max + 1))
        if height_min is not None:
            query = query.where(Actor.height_cm >= height_min)
        if height_max is not None:
            query = query.where(Actor.height_cm <= height_max)
        if cups:
            query = query.where(cls._normalized_cup_expression().in_(cups))
        if has_playable_movies:
            query = query.where(cls._has_playable_movie_expression())
        if search_terms:
            query = query.where(cls._actor_search_conditions(search_terms))

        return query

    @classmethod
    def _require_actor(cls, actor_id: int) -> Actor:
        actor = require_by_id(
            Actor,
            actor_id,
            "actor",
            error_message="演员不存在",
            query=cls.actor_query(),
        )
        if actor.merged_into_id is None:
            return actor
        canonical = (
            cls.actor_query().where(Actor.id == actor.merged_into_id).get_or_none()
        )
        return canonical or actor

    @staticmethod
    def _year_expression():
        return year_expression(Movie.release_date)

    @classmethod
    def list_actors(
        cls,
        gender: ActorListGender = ActorListGender.ALL,
        subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
        age_min: int | None = None,
        age_max: int | None = None,
        height_min: int | None = None,
        height_max: int | None = None,
        cups: Sequence[str] | None = None,
        has_playable_movies: bool = False,
        sort: str | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[ActorResource]:
        start = max(page - 1, 0) * page_size
        search_terms = split_search_terms(query, error_code="invalid_actor_filter")
        filter_kwargs = {
            "gender": gender,
            "subscription_status": subscription_status,
            "age_min": age_min,
            "age_max": age_max,
            "height_min": height_min,
            "height_max": height_max,
            "cups": cups,
            "has_playable_movies": has_playable_movies,
            "search_terms": search_terms,
        }
        total = cls._filtered_actors(**filter_kwargs).count()
        actors = list(
            cls._filtered_actors(**filter_kwargs)
            .order_by(*cls._build_actor_list_sort(sort, search_terms))
            .offset(start)
            .limit(page_size)
        )
        return PageResponse[ActorResource](
            items=[ActorResource.from_actor(actor) for actor in actors],
            page=page,
            page_size=page_size,
            total=total,
        )

    @classmethod
    def get_filter_options(
        cls,
        gender: ActorListGender = ActorListGender.ALL,
        subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
    ) -> ActorFilterOptionsResource:
        today = utc_now_for_db().date()
        scope_conditions = cls._actor_scope_conditions(gender, subscription_status)
        scope_conditions.append(Actor.merged_into.is_null())
        aggregate_query = (
            Actor.select(
                fn.COUNT(Actor.id).alias("actor_count"),
                fn.COUNT(Actor.birthday).alias("birthday_count"),
                fn.MIN(Actor.birthday).alias("oldest_birthday"),
                fn.MAX(Actor.birthday).alias("youngest_birthday"),
                fn.COUNT(Actor.height_cm).alias("height_count"),
                fn.MIN(Actor.height_cm).alias("min_height"),
                fn.MAX(Actor.height_cm).alias("max_height"),
            )
        )
        if scope_conditions:
            aggregate_query = aggregate_query.where(*scope_conditions)
        aggregate = aggregate_query.get()
        normalized_cup = cls._normalized_cup_expression()
        cup_rows = (
            Actor.select(
                normalized_cup.alias("value"), fn.COUNT(Actor.id).alias("count")
            )
            .where(*scope_conditions, normalized_cup.is_null(False))
            .group_by(normalized_cup)
            .order_by(normalized_cup)
        )
        age_min = (
            None
            if aggregate.youngest_birthday is None
            else cls._age_for_birthday(aggregate.youngest_birthday, today)
        )
        age_max = (
            None
            if aggregate.oldest_birthday is None
            else cls._age_for_birthday(aggregate.oldest_birthday, today)
        )
        return ActorFilterOptionsResource(
            actor_count=int(aggregate.actor_count),
            as_of_date=today,
            age=ActorFilterRangeResource(
                min=age_min,
                max=age_max,
                populated_count=int(aggregate.birthday_count),
            ),
            height_cm=ActorFilterRangeResource(
                min=aggregate.min_height,
                max=aggregate.max_height,
                populated_count=int(aggregate.height_count),
            ),
            cups=[
                ActorCupFilterOption(value=row.value, count=int(row.count))
                for row in cup_rows
            ],
        )

    @classmethod
    def search_gfriends(
        cls,
        query: str,
        *,
        limit: int = 20,
    ) -> list[GfriendsActorCandidateResource]:
        normalized_query = query.strip()
        if not normalized_query:
            raise ApiError(422, "invalid_actor_query", "演员搜索词不能为空")
        directory = get_gfriends_directory()
        resources: list[GfriendsActorCandidateResource] = []
        for candidate in directory.search(normalized_query, limit=limit):
            names = [candidate.name, *candidate.alias_names]
            existing = ActorResolutionService.find_existing(names)
            resources.append(
                GfriendsActorCandidateResource(
                    candidate_id=candidate.candidate_id,
                    name=candidate.name,
                    alias_names=list(candidate.alias_names),
                    avatar_url=candidate.avatar_url,
                    existing_actor_id=existing.id if existing is not None else None,
                )
            )
        return resources

    @classmethod
    def import_gfriends_candidate(
        cls,
        payload: GfriendsActorImportRequest,
    ) -> ActorDetailResource:
        try:
            actor = ActorResolutionService.import_gfriends_candidate(
                payload.candidate_id
            )
        except ValueError as exc:
            raise ApiError(
                422,
                "invalid_actor_candidate",
                str(exc),
            ) from exc
        return cls.get_actor_detail(actor.id)

    @classmethod
    def get_actor_detail(cls, actor_id: int) -> ActorDetailResource:
        actor = cls._require_actor(actor_id)
        return ActorDetailResource.from_actor(actor)

    @classmethod
    def create_local_actor(cls, payload: ActorCreateRequest) -> ActorDetailResource:
        """创建本地女优。"""
        actor = Actor.create(
            name=payload.name,
            alias_name=merge_actor_alias_name(
                primary_name=payload.name,
                alias_names=split_actor_alias_name(payload.alias_name or ""),
                existing_alias_name="",
            ),
            gender=payload.gender,
            field_owners={"gender": MANUAL_ACTOR_FIELD_OWNER},
        )
        return cls.get_actor_detail(actor.id)

    @classmethod
    def delete_local_actor(cls, actor_id: int) -> None:
        """删除用户自建女优；被影片引用或是合并目标时拒绝，避免产生隐式数据损失。"""
        with get_database().atomic():
            actor = (
                Actor.select()
                .where(Actor.id == actor_id)
                .for_update()
                .get_or_none()
            )
            if actor is None:
                raise ApiError(404, "actor_not_found", "演员不存在")
            if actor.merged_into_id is not None:
                raise ApiError(
                    422,
                    "actor_merged",
                    "该女优已合并到其他演员，无法删除",
                    {"merged_into_id": actor.merged_into_id},
                )
            if (
                Actor.select(Actor.id)
                .where(Actor.merged_into == actor.id)
                .exists()
            ):
                raise ApiError(
                    409,
                    "actor_in_use",
                    "该女优是其他演员的合并目标，请先处理合并关系",
                    {"reason": "merged_target"},
                )
            linked_movie_count = (
                MovieActor.select(MovieActor.id)
                .where(MovieActor.actor == actor.id)
                .count()
            )
            if linked_movie_count:
                raise ApiError(
                    409,
                    "actor_in_use",
                    "该女优仍被影片引用，请先从影片中移除",
                    {"reason": "movie_links", "movie_count": linked_movie_count},
                )
            images = {
                image.id: image
                for image in (actor.profile_image, actor.profile_image_override)
                if image is not None
            }
            actor.delete_instance()

        obsolete_paths: set[str] = set()
        for image in images.values():
            obsolete_paths.update(
                ImageCleanupService.delete_image_record_if_unused(image)
            )
        ImageCleanupService.delete_obsolete_image_files(obsolete_paths)

    @classmethod
    def update_profile(
        cls,
        actor_id: int,
        payload: ActorUpdateRequest,
    ) -> ActorDetailResource:
        actor = cls._require_actor(actor_id)
        changes = payload.model_dump(
            exclude_unset=True,
        )
        if not changes:
            raise ApiError(422, "empty_actor_update", "至少需要修改一个资料字段")
        if payload.birthday is not None and payload.birthday > utc_now_for_db().date():
            raise ApiError(422, "invalid_actor_profile", "birthday 不能晚于今天")

        editable_fields = (
            cls.ACTOR_PROFILE_EDITABLE_FIELDS | cls.LOCAL_ACTOR_IDENTITY_FIELDS
        )
        unsupported_fields = set(changes) - editable_fields
        if unsupported_fields:
            raise ApiError(
                422,
                "invalid_actor_update",
                "包含不支持修改的女优字段",
                {"fields": sorted(unsupported_fields)},
            )

        identity_fields = set(changes) & cls.LOCAL_ACTOR_IDENTITY_FIELDS
        if identity_fields:
            effective_name = changes.get("name") or actor.name
            alias_names = split_actor_alias_name(
                changes.get("alias_name", actor.alias_name)
            )
            changes["alias_name"] = merge_actor_alias_name(
                primary_name=effective_name,
                alias_names=alias_names,
                existing_alias_name="",
            )

        assignments = [f"{field} = %s" for field in changes]
        params = list(changes.values())
        scalar_fields = set(changes) & (cls.ACTOR_PROFILE_EDITABLE_FIELDS - {"display_name_override"})
        if scalar_fields:
            assignments.append("field_owners = field_owners || %s::jsonb")
            params.append(
                json.dumps(
                    {field: "host:manual" for field in scalar_fields},
                    ensure_ascii=False,
                )
            )
        if scalar_fields:
            assignments.append("mutation_revision = mutation_revision + 1")
        assignments.append("updated_at = now()")
        params.append(actor.id)
        cursor = get_database().execute_sql(
            f"""
            UPDATE actor SET {", ".join(assignments)}
            WHERE id = %s
            """,
            params,
        )
        if cursor.rowcount != 1:
            raise ApiError(404, "actor_not_found", "演员不存在")
        return cls.get_actor_detail(actor.id)

    @classmethod
    def _render_profile_image_webp(cls, content: bytes, content_type: str | None) -> bytes:
        """校验并归一化头像图片为 webp 字节；人工上传与插件写入共用。"""
        if len(content) > cls.ACTOR_PROFILE_IMAGE_MAX_BYTES:
            raise ApiError(413, "actor_profile_image_too_large", "头像图片不能超过 10 MiB")
        normalized_content_type = (content_type or "").lower().strip()
        if normalized_content_type and normalized_content_type not in cls.ACTOR_PROFILE_IMAGE_CONTENT_TYPES:
            raise ApiError(422, "invalid_actor_profile_image", "只支持 JPEG、PNG 或 WebP 图片")
        buffer = BytesIO()
        try:
            with PillowImage.open(BytesIO(content)) as source:
                if source.format not in cls.ACTOR_PROFILE_IMAGE_FORMATS:
                    raise ApiError(422, "invalid_actor_profile_image", "只支持 JPEG、PNG 或 WebP 图片")
                if max(source.size) > cls.ACTOR_PROFILE_IMAGE_MAX_DIMENSION:
                    raise ApiError(422, "invalid_actor_profile_image", "头像图片边长不能超过 4096 像素")
                normalized = ImageOps.exif_transpose(source)
                try:
                    normalized.thumbnail(
                        (
                            cls.ACTOR_PROFILE_IMAGE_OUTPUT_DIMENSION,
                            cls.ACTOR_PROFILE_IMAGE_OUTPUT_DIMENSION,
                        ),
                        PillowImage.Resampling.LANCZOS,
                    )
                    if normalized.mode not in {"RGB", "RGBA"}:
                        normalized = normalized.convert("RGBA" if "A" in normalized.getbands() else "RGB")
                    normalized.save(buffer, format="WEBP", quality=90, method=6)
                finally:
                    if normalized is not source:
                        normalized.close()
        except ApiError:
            raise
        except (
            OSError,
            UnidentifiedImageError,
            PillowImage.DecompressionBombError,
            ValueError,
        ) as exc:
            raise ApiError(422, "invalid_actor_profile_image", "无法读取或处理头像图片") from exc
        return buffer.getvalue()

    @classmethod
    def _write_profile_image_file(cls, relative_path: Path, content: bytes) -> None:
        """把归一化后的头像写到正式图片目录：先写临时目录再原子替换。"""
        image_root = media_image_root_path()
        image_root.mkdir(parents=True, exist_ok=True)
        temp_root = Path(tempfile.mkdtemp(prefix=".actor-profile-", dir=image_root))
        try:
            temp_path = temp_root / "avatar.webp"
            temp_path.write_bytes(content)
            final_path = image_root / relative_path
            final_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.replace(final_path)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)

    @staticmethod
    def _cleanup_profile_override(old_override: Image | None) -> None:
        if old_override is not None:
            obsolete_paths = ImageCleanupService.delete_image_record_if_unused(old_override)
            ImageCleanupService.delete_obsolete_image_files(obsolete_paths)

    @classmethod
    def upload_profile_image(
        cls,
        actor_id: int,
        *,
        content: bytes,
        content_type: str | None,
    ) -> ActorDetailResource:
        """人工上传头像覆盖：可覆盖任何插件 owner，图片归属记为人工。"""
        actor = cls._require_actor(actor_id)
        rendered = cls._render_profile_image_webp(content, content_type)
        relative_path = Path("actors") / "manual" / f"{actor.id}-{uuid4().hex}.webp"
        cls._write_profile_image_file(relative_path, rendered)
        old_override = actor.profile_image_override if actor.profile_image_override_id else None
        try:
            with get_database().atomic():
                image = Image.create(origin=relative_path.as_posix())
                if not ActorOwnershipGateway.set_manual_profile_image_override(
                    actor.id, image.id
                ):
                    raise ApiError(404, "actor_not_found", "演员不存在")
        except Exception:
            (media_image_root_path() / relative_path).unlink(missing_ok=True)
            raise
        cls._cleanup_profile_override(old_override)
        return cls.get_actor_detail(actor.id)

    @classmethod
    def set_profile_image_override(
        cls,
        actor_id: int,
        plugin_id: str,
        image_path: str,
        expected_revision: int,
    ) -> bool:
        """插件写头像覆盖；人工覆盖或别的插件已持有时返回 False 且零修改。"""
        from src.plugins.image_delivery import resolve_plugin_image_path

        actor = Actor.resolve_canonical(actor_id)
        if actor is None:
            return False
        content = resolve_plugin_image_path(plugin_id, image_path).read_bytes()
        try:
            rendered = cls._render_profile_image_webp(content, None)
        except ApiError as exc:
            raise ValueError(exc.message) from exc
        relative_path = (
            Path("actors")
            / "plugin"
            / normalize_asset_dir_name(plugin_id)
            / f"{actor.id}-{uuid4().hex}.webp"
        )
        cls._write_profile_image_file(relative_path, rendered)
        old_override = actor.profile_image_override if actor.profile_image_override_id else None
        try:
            with get_database().atomic():
                image = Image.create(origin=relative_path.as_posix())
                if not ActorOwnershipGateway.replace_profile_image_override(
                    actor.id, plugin_id, image.id, expected_revision
                ):
                    raise _ProfileImageWriteConflict()
        except _ProfileImageWriteConflict:
            (media_image_root_path() / relative_path).unlink(missing_ok=True)
            return False
        except Exception:
            (media_image_root_path() / relative_path).unlink(missing_ok=True)
            raise
        cls._cleanup_profile_override(old_override)
        return True

    @classmethod
    def clear_profile_image_override(
        cls,
        actor_id: int,
        plugin_id: str,
        expected_revision: int,
    ) -> bool:
        """插件移除自己的头像覆盖；人工覆盖保持不变。"""
        actor = Actor.resolve_canonical(actor_id)
        if actor is None:
            return False
        old_override = actor.profile_image_override if actor.profile_image_override_id else None
        if not ActorOwnershipGateway.clear_profile_image_override(
            actor.id, plugin_id, expected_revision
        ):
            return False
        cls._cleanup_profile_override(old_override)
        return True

    @classmethod
    def clear_profile_image(
        cls,
        actor_id: int,
    ) -> ActorDetailResource:
        actor = cls._require_actor(actor_id)
        old_override = actor.profile_image_override if actor.profile_image_override_id else None
        if old_override is None:
            return cls.get_actor_detail(actor.id)
        if not ActorOwnershipGateway.clear_manual_profile_image_override(actor.id):
            raise ApiError(404, "actor_not_found", "演员不存在")
        cls._cleanup_profile_override(old_override)
        return cls.get_actor_detail(actor.id)

    @classmethod
    def set_subscription(cls, actor_id: int, subscribed: bool) -> None:
        actor = cls._require_actor(actor_id)

        # 演员订阅当前作为收藏标记；远端上新同步能力后续由独立来源提供。
        if subscribed:
            actor.is_subscribed = True
            if actor.subscribed_at is None:
                actor.subscribed_at = utc_now_for_db()
        else:
            actor.is_subscribed = False
            actor.subscribed_at = None
        actor.save(only=[Actor.is_subscribed, Actor.subscribed_at])

    @classmethod
    def get_actor_movie_ids(cls, actor_id: int) -> list[int]:
        actor = cls._require_actor(actor_id)
        query = (
            Movie.select(Movie.id)
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor.id)
            .order_by(Movie.id)
        )
        return [movie.id for movie in query]

    @classmethod
    def get_actor_tags(cls, actor_id: int) -> list[TagListItemResource]:
        actor = cls._require_actor(actor_id)
        movie_count = fn.COUNT(fn.DISTINCT(MovieTag.movie)).alias("movie_count")
        query = (
            Tag.select(Tag, movie_count)
            .join(MovieTag)
            .join(Movie, on=(MovieTag.movie == Movie.id))
            .join(MovieActor, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor.id)
            .group_by(Tag.id)
            .order_by(Tag.name)
        )
        return TagListItemResource.from_items(query)

    @classmethod
    def get_actor_years(cls, actor_id: int) -> list[YearResource]:
        actor = cls._require_actor(actor_id)
        year_expression = cls._year_expression()
        query = (
            Movie.select(
                year_expression.alias("year"),
                fn.COUNT(Movie.id).alias("movie_count"),
            )
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(
                MovieActor.actor == actor.id,
                Movie.release_date.is_null(False),
            )
            .group_by(year_expression)
            .order_by(year_expression.desc())
        )
        years = []
        for row in query:
            # 不同数据库对年份表达式的返回类型不完全一致，统一转成 int 再交给 schema。
            year = row.year
            if year is None:
                continue
            years.append(YearResource(year=int(year), movie_count=int(row.movie_count)))
        return years
