"""演员目录 service。

负责演员列表、详情、关联影片标识/标签/年份查询，以及按演员名从 JavDB 搜索并导入。
阅读入口建议从 ``list_actors``、``get_actor_movie_ids``、``stream_search_and_upsert_actor_from_javdb`` 开始。
"""

import json
import shutil
import tempfile
from calendar import monthrange
from collections.abc import Iterator, Sequence
from datetime import date
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from loguru import logger
from peewee import JOIN, fn
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError

from src.api.exception.errors import ApiError
from src.common.media_paths import media_image_root_path
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import (
    build_ordered_expressions,
    require_by_id,
    resolve_sort_expression,
)
from src.metadata._providers.models import JavdbMovieActorResource
from src.metadata.factory import build_javdb_provider
from src.metadata.provider import MetadataNotFoundError
from src.model import Actor, Image, Movie, MovieActor, MovieTag, Tag, get_database
from src.model.expressions import year_expression
from src.schema.catalog.actors import (
    ActorCupFilterOption,
    ActorDetailResource,
    ActorFilterOptionsResource,
    ActorFilterRangeResource,
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorResource,
    ActorUpdateRequest,
    YearResource,
)
from src.schema.catalog.movies import TagResource
from src.schema.common.pagination import PageResponse
from src.service.catalog.catalog_import_service import (
    CatalogImportService,
    ImageDownloadError,
)
from src.service.catalog.image_cleanup_service import ImageCleanupService


class ActorService:
    """聚合 Actor 查询和 JavDB 演员导入流程。"""

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
            "age": Actor.birthday,
            "height_cm": Actor.height_cm,
            "bust_cm": Actor.bust_cm,
            "waist_cm": Actor.waist_cm,
            "hips_cm": Actor.hips_cm,
            "waist_hip_ratio": cls._waist_hip_ratio_expression(),
            "cup": cls._normalized_cup_expression(),
        }

    @classmethod
    def _build_actor_list_sort(cls, sort: str | None) -> Sequence:
        """解析演员列表排序表达式，并补充稳定的 id 次级排序。"""

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
    def _actor_query():
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
    def _filtered_actors(
        cls,
        gender: ActorListGender = ActorListGender.ALL,
        subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
        age_min: int | None = None,
        age_max: int | None = None,
        height_min: int | None = None,
        height_max: int | None = None,
        cups: Sequence[str] | None = None,
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

        query = cls._actor_query()
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

        return query

    @classmethod
    def _require_actor(cls, actor_id: int) -> Actor:
        return require_by_id(
            Actor,
            actor_id,
            "actor",
            error_message="演员不存在",
            query=cls._actor_query(),
        )

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
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[ActorResource]:
        start = max(page - 1, 0) * page_size
        filter_kwargs = {
            "gender": gender,
            "subscription_status": subscription_status,
            "age_min": age_min,
            "age_max": age_max,
            "height_min": height_min,
            "height_max": height_max,
            "cups": cups,
        }
        total = cls._filtered_actors(**filter_kwargs).count()
        actors = list(
            cls._filtered_actors(**filter_kwargs)
            .order_by(*cls._build_actor_list_sort(sort))
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
    def _build_catalog_import_service(cls) -> CatalogImportService:
        return CatalogImportService()

    @classmethod
    def stream_search_and_upsert_actor_from_javdb(
        cls,
        actor_name: str,
    ) -> Iterator[tuple[str, dict]]:
        """按 SSE 事件顺序输出演员搜索和导入进度。"""
        normalized_name = actor_name.strip()
        yield "search_started", {"actor_name": normalized_name}

        try:
            actor_resources = build_javdb_provider().search_actors(normalized_name)
        except MetadataNotFoundError:
            yield (
                "completed",
                {"success": False, "reason": "actor_not_found", "actors": []},
            )
            return
        except Exception as exc:
            logger.exception(
                "Javdb actor search failed actor_name={} detail={}",
                normalized_name,
                exc,
            )
            yield (
                "completed",
                {"success": False, "reason": "internal_error", "actors": []},
            )
            return

        # JavDB 搜索结果可能包含重复演员卡片，这里先按 javdb_id 去重，再进入导入阶段。
        deduplicated_resources: list[JavdbMovieActorResource] = []
        seen_javdb_ids: set[str] = set()
        for actor_resource in actor_resources:
            if actor_resource.javdb_id in seen_javdb_ids:
                continue
            seen_javdb_ids.add(actor_resource.javdb_id)
            deduplicated_resources.append(actor_resource)

        total = len(deduplicated_resources)
        yield (
            "actor_found",
            {
                "actors": [
                    {
                        "javdb_id": actor_resource.javdb_id,
                        "name": actor_resource.name,
                        "avatar_url": actor_resource.avatar_url,
                    }
                    for actor_resource in deduplicated_resources
                ],
                "total": total,
            },
        )

        yield "upsert_started", {"total": total}

        created_count = 0
        already_exists_count = 0
        failed_count = 0
        failed_items: list[dict] = []
        imported_actors: list[ActorResource] = []
        import_service = cls._build_catalog_import_service()

        for index, actor_resource in enumerate(deduplicated_resources, start=1):
            # 图片下载是前端最关心的慢步骤，单独发事件便于展示进度。
            yield (
                "image_download_started",
                {
                    "javdb_id": actor_resource.javdb_id,
                    "index": index,
                    "total": total,
                },
            )
            existed_before_upsert = (
                Actor.get_or_none(Actor.javdb_id == actor_resource.javdb_id) is not None
            )
            try:
                actor = import_service.upsert_actor_from_javdb_resource(actor_resource)
                actor_with_profile = (
                    cls._actor_query().where(Actor.id == actor.id).get_or_none()
                    or actor
                )
                imported_actors.append(
                    ActorResource.from_actor(actor_with_profile)
                )
                if existed_before_upsert:
                    already_exists_count += 1
                else:
                    created_count += 1
                yield (
                    "image_download_finished",
                    {
                        "javdb_id": actor_resource.javdb_id,
                        "index": index,
                        "total": total,
                        "has_avatar": bool(actor_resource.avatar_url),
                    },
                )
            except ImageDownloadError as exc:
                failed_count += 1
                logger.warning(
                    "Javdb actor image download failed actor_name={} javdb_id={} detail={}",
                    normalized_name,
                    actor_resource.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": actor_resource.javdb_id,
                        "reason": "image_download_failed",
                        "detail": str(exc),
                    }
                )
            except Exception as exc:
                failed_count += 1
                logger.exception(
                    "Javdb actor upsert failed actor_name={} javdb_id={} detail={}",
                    normalized_name,
                    actor_resource.javdb_id,
                    exc,
                )
                failed_items.append(
                    {
                        "javdb_id": actor_resource.javdb_id,
                        "reason": "upsert_failed",
                        "detail": str(exc),
                    }
                )

        stats = {
            "total": total,
            "created_count": created_count,
            "already_exists_count": already_exists_count,
            "failed_count": failed_count,
        }
        yield "upsert_finished", stats

        if imported_actors:
            yield (
                "completed",
                {
                    "success": True,
                    "actors": [actor.model_dump() for actor in imported_actors],
                    "failed_items": failed_items,
                    "stats": stats,
                },
            )
            return

        yield (
            "completed",
            {
                "success": False,
                "reason": "internal_error",
                "actors": [],
                "failed_items": failed_items,
                "stats": stats,
            },
        )

    @classmethod
    def get_actor_detail(cls, actor_id: int) -> ActorDetailResource:
        actor = cls._require_actor(actor_id)
        return ActorDetailResource.from_actor(actor)

    @classmethod
    def update_profile(
        cls,
        actor_id: int,
        payload: ActorUpdateRequest,
    ) -> ActorDetailResource:
        cls._require_actor(actor_id)
        changes = payload.model_dump(
            exclude_unset=True,
        )
        if not changes:
            raise ApiError(422, "empty_actor_update", "至少需要修改一个资料字段")
        if payload.birthday is not None and payload.birthday > utc_now_for_db().date():
            raise ApiError(422, "invalid_actor_profile", "birthday 不能晚于今天")

        unsupported_fields = set(changes) - cls.ACTOR_PROFILE_EDITABLE_FIELDS
        if unsupported_fields:
            raise ApiError(
                422,
                "invalid_actor_update",
                "包含不支持修改的女优字段",
                {"fields": sorted(unsupported_fields)},
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
        params.append(actor_id)
        cursor = get_database().execute_sql(
            f"""
            UPDATE actor SET {", ".join(assignments)}
            WHERE id = %s
            """,
            params,
        )
        if cursor.rowcount != 1:
            raise ApiError(404, "actor_not_found", "演员不存在")
        return cls.get_actor_detail(actor_id)

    @classmethod
    def upload_profile_image(
        cls,
        actor_id: int,
        *,
        content: bytes,
        content_type: str | None,
    ) -> ActorDetailResource:
        actor = cls._require_actor(actor_id)
        if len(content) > cls.ACTOR_PROFILE_IMAGE_MAX_BYTES:
            raise ApiError(413, "actor_profile_image_too_large", "头像图片不能超过 10 MiB")
        normalized_content_type = (content_type or "").lower().strip()
        if normalized_content_type and normalized_content_type not in cls.ACTOR_PROFILE_IMAGE_CONTENT_TYPES:
            raise ApiError(422, "invalid_actor_profile_image", "只支持 JPEG、PNG 或 WebP 图片")

        image_root = media_image_root_path()
        image_root.mkdir(parents=True, exist_ok=True)
        temp_root = Path(tempfile.mkdtemp(prefix=".actor-profile-upload-", dir=image_root))
        relative_path = Path("actors") / "manual" / f"{actor.id}-{uuid4().hex}.webp"
        final_path = image_root / relative_path
        committed = False
        old_override = actor.profile_image_override if actor.profile_image_override_id else None
        try:
            temp_path = temp_root / "avatar.webp"
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
                    normalized.save(temp_path, format="WEBP", quality=90, method=6)
                finally:
                    if normalized is not source:
                        normalized.close()

            final_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.replace(final_path)
            with get_database().atomic():
                image = Image.create(
                    origin=relative_path.as_posix(),
                    small=relative_path.as_posix(),
                    medium=relative_path.as_posix(),
                    large=relative_path.as_posix(),
                )
                cursor = get_database().execute_sql(
                    """
                    UPDATE actor
                    SET profile_image_override_id = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    [image.id, actor_id],
                )
                if cursor.rowcount != 1:
                    raise ApiError(404, "actor_not_found", "演员不存在")
            committed = True
        except ApiError:
            raise
        except (
            OSError,
            UnidentifiedImageError,
            PillowImage.DecompressionBombError,
            ValueError,
        ) as exc:
            raise ApiError(422, "invalid_actor_profile_image", "无法读取或处理头像图片") from exc
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
            if not committed:
                final_path.unlink(missing_ok=True)

        if old_override is not None:
            obsolete_paths = ImageCleanupService.delete_image_record_if_unused(old_override)
            ImageCleanupService.delete_obsolete_image_files(obsolete_paths)
        return cls.get_actor_detail(actor_id)

    @classmethod
    def clear_profile_image(
        cls,
        actor_id: int,
    ) -> ActorDetailResource:
        actor = cls._require_actor(actor_id)
        old_override = actor.profile_image_override if actor.profile_image_override_id else None
        if old_override is None:
            return cls.get_actor_detail(actor_id)
        cursor = get_database().execute_sql(
            """
            UPDATE actor
            SET profile_image_override_id = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            [actor_id],
        )
        if cursor.rowcount != 1:
            raise ApiError(404, "actor_not_found", "演员不存在")
        obsolete_paths = ImageCleanupService.delete_image_record_if_unused(old_override)
        ImageCleanupService.delete_obsolete_image_files(obsolete_paths)
        return cls.get_actor_detail(actor_id)

    @classmethod
    def set_subscription(cls, actor_id: int, subscribed: bool) -> None:
        actor = require_by_id(Actor, actor_id, "actor", error_message="演员不存在")

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
        cls._require_actor(actor_id)
        query = (
            Movie.select(Movie.id)
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor_id)
            .order_by(Movie.id)
        )
        return [movie.id for movie in query]

    @classmethod
    def get_actor_tags(cls, actor_id: int) -> list[TagResource]:
        cls._require_actor(actor_id)
        query = (
            Tag.select(Tag)
            .join(MovieTag)
            .join(Movie, on=(MovieTag.movie == Movie.id))
            .join(MovieActor, on=(MovieActor.movie == Movie.id))
            .where(MovieActor.actor == actor_id)
            .distinct()
            .order_by(Tag.name)
        )
        return [TagResource(tag_id=tag.id, name=tag.name) for tag in query]

    @classmethod
    def get_actor_years(cls, actor_id: int) -> list[YearResource]:
        cls._require_actor(actor_id)
        year_expression = cls._year_expression()
        query = (
            Movie.select(
                year_expression.alias("year"),
                fn.COUNT(Movie.id).alias("movie_count"),
            )
            .join(MovieActor, JOIN.INNER, on=(MovieActor.movie == Movie.id))
            .where(
                MovieActor.actor == actor_id,
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
