from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import peewee
from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.common.service_helpers import (
    build_ordered_expressions,
    require_by_id,
    resolve_sort_expression,
)
from src.model import Media, MediaMediaTag, MediaTag, get_database
from src.schema.common.media_tags import MediaTagBriefResource
from src.schema.playback.media_tags import (
    MediaTagAssignmentResource,
    MediaTagPreviewMatchResource,
    MediaTagResource,
    MediaTagUpsertRequest,
)
from src.service.playback.operation_locks import (
    MEDIA_LOCK,
    media_operation_lock,
)


@dataclass(frozen=True, slots=True)
class MediaTagRule:
    tag_id: int
    name: str
    include_terms: tuple[str, ...]
    exclude_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MediaTagMatch:
    tag_id: int
    name: str
    matched_term: str


def normalize_media_tag_text(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def _normalized_term_key(value: str) -> str:
    return normalize_media_tag_text(value.strip())


class MediaTagService:
    @staticmethod
    def _normalize_terms(
        terms: Iterable[str],
        *,
        field_name: str,
        required: bool = False,
    ) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_term in terms:
            term = str(raw_term).strip()
            if not term:
                raise ApiError(
                    422,
                    "invalid_media_tag_filter",
                    "媒体标签词条不能为空",
                    {field_name: list(terms)},
                )
            if len(term) > 100:
                raise ApiError(
                    422,
                    "invalid_media_tag_filter",
                    "媒体标签词条不能超过 100 个字符",
                    {field_name: term},
                )
            key = _normalized_term_key(term)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(term)
        if required and not normalized:
            raise ApiError(
                422,
                "invalid_media_tag_filter",
                "媒体标签至少需要一个命中词",
                {field_name: []},
            )
        if len(normalized) > 50:
            raise ApiError(
                422,
                "invalid_media_tag_filter",
                "媒体标签词条不能超过 50 条",
                {field_name: len(normalized)},
            )
        return normalized

    @staticmethod
    def _normalize_query(query: str | None) -> str | None:
        if query is None:
            return None
        normalized = query.strip()
        if not normalized:
            raise ApiError(
                422,
                "invalid_media_tag_filter",
                "无效的媒体标签筛选",
                {"query": query},
            )
        return normalized

    @staticmethod
    def _build_sort(sort: str | None):
        def _media_count_order(_field_name: str, direction: str) -> list:
            return build_ordered_expressions(
                fn.COUNT(Media.id),
                direction,
                tie_breaker=MediaTag.id,
                extra=(MediaTag.name.asc(),),
            )

        return resolve_sort_expression(
            (sort or "").strip() or "media_count:desc",
            {
                "name": MediaTag.name,
                "media_count": fn.COUNT(Media.id),
            },
            error_code="invalid_media_tag_filter",
            tie_breaker=MediaTag.id,
            extra_sort_builders={"media_count": _media_count_order},
        )

    @classmethod
    def list_tags(
        cls,
        *,
        query: str | None = None,
        sort: str | None = None,
    ) -> list[MediaTagResource]:
        media_count = fn.COUNT(Media.id).alias("media_count")
        resource_query = (
            MediaTag.select(MediaTag, media_count)
            .join(
                MediaMediaTag,
                JOIN.LEFT_OUTER,
                on=(MediaMediaTag.tag == MediaTag.id),
            )
            .join(
                Media,
                JOIN.LEFT_OUTER,
                on=(
                    (MediaMediaTag.media == Media.id)
                    & (Media.valid == True)
                    & (Media.movie.is_null(False))
                ),
            )
            .group_by(MediaTag.id)
            .order_by(*cls._build_sort(sort))
        )
        normalized_query = cls._normalize_query(query)
        if normalized_query is not None:
            resource_query = resource_query.where(MediaTag.name.contains(normalized_query))
        return MediaTagResource.from_items(resource_query)

    @classmethod
    def _require_tag(cls, tag_id: int) -> MediaTag:
        tag = MediaTag.get_or_none(MediaTag.id == tag_id)
        if tag is None:
            raise ApiError(
                404,
                "media_tag_not_found",
                "媒体标签不存在",
                {"tag_id": tag_id},
            )
        return tag

    @classmethod
    def _ensure_name_available(cls, name: str, *, exclude_tag_id: int | None = None) -> None:
        condition = MediaTag.name == name
        if exclude_tag_id is not None:
            condition &= MediaTag.id != exclude_tag_id
        if MediaTag.get_or_none(condition) is not None:
            raise ApiError(
                409,
                "media_tag_name_conflict",
                "媒体标签名称已存在",
                {"name": name},
            )

    @classmethod
    def create_tag(cls, payload: MediaTagUpsertRequest) -> MediaTagResource:
        include_terms = cls._normalize_terms(
            payload.include_terms, field_name="include_terms", required=True
        )
        exclude_terms = cls._normalize_terms(
            payload.exclude_terms, field_name="exclude_terms"
        )
        cls._ensure_name_available(payload.name)
        try:
            tag = MediaTag.create(
                name=payload.name,
                include_terms=include_terms,
                exclude_terms=exclude_terms,
                enabled=payload.enabled,
            )
        except peewee.IntegrityError as exc:
            raise ApiError(
                409,
                "media_tag_name_conflict",
                "媒体标签名称已存在",
                {"name": payload.name},
            ) from exc
        return MediaTagResource.from_peewee_model(
            tag,
            extra={"media_count": 0},
        )

    @classmethod
    def update_tag(
        cls, tag_id: int, payload: MediaTagUpsertRequest
    ) -> MediaTagResource:
        tag = cls._require_tag(tag_id)
        include_terms = cls._normalize_terms(
            payload.include_terms, field_name="include_terms", required=True
        )
        exclude_terms = cls._normalize_terms(
            payload.exclude_terms, field_name="exclude_terms"
        )
        cls._ensure_name_available(payload.name, exclude_tag_id=tag.id)
        tag.name = payload.name
        tag.include_terms = include_terms
        tag.exclude_terms = exclude_terms
        tag.enabled = payload.enabled
        try:
            tag.save(
                only=[
                    MediaTag.name,
                    MediaTag.include_terms,
                    MediaTag.exclude_terms,
                    MediaTag.enabled,
                ]
            )
        except peewee.IntegrityError as exc:
            raise ApiError(
                409,
                "media_tag_name_conflict",
                "媒体标签名称已存在",
                {"name": payload.name},
            ) from exc
        media_count = (
            MediaMediaTag.select()
            .join(Media, on=(MediaMediaTag.media == Media.id))
            .where(
                MediaMediaTag.tag == tag.id,
                Media.valid == True,
                Media.movie.is_null(False),
            )
            .count()
        )
        return MediaTagResource.from_peewee_model(
            tag,
            extra={"media_count": media_count},
        )

    @classmethod
    def delete_tag(cls, tag_id: int) -> None:
        tag = cls._require_tag(tag_id)
        tag.delete_instance()

    @classmethod
    def load_active_rules(cls) -> tuple[MediaTagRule, ...]:
        return tuple(
            MediaTagRule(
                tag_id=tag.id,
                name=tag.name,
                include_terms=tuple(tag.include_terms or ()),
                exclude_terms=tuple(tag.exclude_terms or ()),
            )
            for tag in (
                MediaTag.select()
                .where(MediaTag.enabled == True)
                .order_by(MediaTag.id)
            )
        )

    @staticmethod
    def match_rules(
        rules: Sequence[MediaTagRule],
        *,
        file_name: str = "",
        release_title: str | None = None,
        release_description: str | None = None,
    ) -> list[MediaTagMatch]:
        match_text = normalize_media_tag_text(
            " ".join(
                part
                for part in (file_name, release_title or "", release_description or "")
                if part
            )
        )
        matches: list[MediaTagMatch] = []
        for rule in rules:
            if any(
                _normalized_term_key(term) in match_text
                for term in rule.exclude_terms
                if term.strip()
            ):
                continue
            matched_term = next(
                (
                    term
                    for term in rule.include_terms
                    if term.strip() and _normalized_term_key(term) in match_text
                ),
                None,
            )
            if matched_term is not None:
                matches.append(
                    MediaTagMatch(
                        tag_id=rule.tag_id,
                        name=rule.name,
                        matched_term=matched_term,
                    )
                )
        return matches

    @classmethod
    def preview(
        cls,
        *,
        file_name: str,
        release_title: str | None = None,
        release_description: str | None = None,
    ) -> list[MediaTagPreviewMatchResource]:
        return [
            MediaTagPreviewMatchResource(
                tag_id=match.tag_id,
                name=match.name,
                matched_term=match.matched_term,
            )
            for match in cls.match_rules(
                cls.load_active_rules(),
                file_name=file_name,
                release_title=release_title,
                release_description=release_description,
            )
        ]

    @classmethod
    def replace_media_tags(
        cls,
        media: Media,
        rules: Sequence[MediaTagRule],
        *,
        release_title: str | None = None,
        release_description: str | None = None,
    ) -> set[int]:
        matches = cls.match_rules(
            rules,
            file_name=media.file_name,
            release_title=release_title,
            release_description=release_description,
        )
        MediaMediaTag.delete().where(MediaMediaTag.media == media.id).execute()
        if not matches:
            return set()
        MediaMediaTag.insert_many(
            [{"media": media.id, "tag": match.tag_id} for match in matches]
        ).execute()
        return {match.tag_id for match in matches}

    @classmethod
    def attach_matching_tags(
        cls,
        media: Media,
        rules: Sequence[MediaTagRule],
        *,
        release_title: str | None = None,
        release_description: str | None = None,
    ) -> set[int]:
        matches = cls.match_rules(
            rules,
            file_name=media.file_name,
            release_title=release_title,
            release_description=release_description,
        )
        if not matches:
            return set()
        MediaMediaTag.insert_many(
            [{"media": media.id, "tag": match.tag_id} for match in matches]
        ).on_conflict_ignore().execute()
        return {match.tag_id for match in matches}

    @staticmethod
    def list_briefs_by_media_ids(
        media_ids: Sequence[int],
    ) -> dict[int, list[MediaTagBriefResource]]:
        if not media_ids:
            return {}
        rows = (
            MediaMediaTag.select(
                MediaMediaTag.media.alias("media_id"),
                MediaTag.id.alias("id"),
                MediaTag.name,
            )
            .join(MediaTag)
            .where(MediaMediaTag.media.in_(set(media_ids)))
            .order_by(MediaMediaTag.media, MediaTag.name, MediaTag.id)
            .dicts()
        )
        result: dict[int, list[MediaTagBriefResource]] = {}
        for row in rows:
            result.setdefault(int(row["media_id"]), []).append(
                MediaTagBriefResource.model_validate(row)
            )
        return result

    @staticmethod
    def _require_jav_media(media_id: int) -> Media:
        media = require_by_id(
            Media,
            media_id,
            "media",
            error_message="媒体不存在",
        )
        if media.movie_number is None:
            raise ApiError(
                422,
                "media_tag_not_supported",
                "媒体文件标签仅支持 JAV 媒体",
                {"media_id": media_id},
            )
        if not media.valid:
            raise ApiError(
                422,
                "media_tag_assignment_invalid",
                "失效媒体不支持媒体文件标签",
                {"media_id": media_id},
            )
        return media

    @staticmethod
    def _require_assignment_tags(tag_ids: Sequence[int]) -> list[MediaTag]:
        # 合法性（正整数、去重）由 MediaTagAssignmentRequest 的校验器保证，
        # 这里只负责标签是否存在。
        if not tag_ids:
            return []
        tags = list(
            MediaTag.select()
            .where(MediaTag.id.in_(list(tag_ids)))
            .order_by(MediaTag.name, MediaTag.id)
        )
        found_ids = {tag.id for tag in tags}
        missing_ids = [tag_id for tag_id in tag_ids if tag_id not in found_ids]
        if missing_ids:
            raise ApiError(
                404,
                "media_tag_not_found",
                "媒体标签不存在",
                {"tag_ids": missing_ids},
            )
        return tags

    @classmethod
    def _assignment_resource(cls, media: Media) -> MediaTagAssignmentResource:
        tags = cls.list_briefs_by_media_ids([media.id]).get(media.id, [])
        return MediaTagAssignmentResource(
            media_id=media.id,
            tags=tags,
            is_media_tags_manually_managed=media.is_media_tags_manually_managed,
        )

    @classmethod
    def get_media_assignment(cls, media_id: int) -> MediaTagAssignmentResource:
        return cls._assignment_resource(cls._require_jav_media(media_id))

    @classmethod
    def set_manual_assignment(
        cls,
        media_id: int,
        tag_ids: Sequence[int],
    ) -> MediaTagAssignmentResource:
        # 与重算任务共用同一条媒体锁，避免手工接管被自动打标覆盖。
        with media_operation_lock(MEDIA_LOCK, media_id):
            media = cls._require_jav_media(media_id)
            tags = cls._require_assignment_tags(tag_ids)
            with get_database().atomic():
                MediaMediaTag.delete().where(MediaMediaTag.media == media.id).execute()
                if tags:
                    MediaMediaTag.insert_many(
                        [{"media": media.id, "tag": tag.id} for tag in tags]
                    ).on_conflict_ignore().execute()
                media.is_media_tags_manually_managed = True
                media.save(only=[Media.is_media_tags_manually_managed])
            return cls._assignment_resource(media)

    @classmethod
    def reset_media_assignment_to_auto(
        cls,
        media_id: int,
    ) -> MediaTagAssignmentResource:
        with media_operation_lock(MEDIA_LOCK, media_id):
            media = cls._require_jav_media(media_id)
            rules = cls.load_active_rules()
            with get_database().atomic():
                media.is_media_tags_manually_managed = False
                media.save(only=[Media.is_media_tags_manually_managed])
                cls.replace_media_tags(
                    media,
                    rules,
                    release_title=media.release_title,
                    release_description=media.release_description,
                )
            return cls._assignment_resource(media)
