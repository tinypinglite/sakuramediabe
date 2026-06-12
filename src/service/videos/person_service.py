"""人物（Person）service：非 JAV 视频的人物维度增删改查。"""

from datetime import datetime

from peewee import JOIN, fn

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.common.service_helpers import require_record, validate_page
from src.model import Person, VideoItemPerson
from src.schema.catalog.actors import ImageResource
from src.schema.common.pagination import PageResponse
from src.schema.videos.persons import (
    PersonCreateRequest,
    PersonResource,
    PersonUpdateRequest,
)

# 人物列表允许的排序字段，按 video_count / name / created_at。
_PERSON_SORT_FIELDS = {
    "video_count": "video_count",
    "name": "name",
    "created_at": "created_at",
}


class PersonService:
    @staticmethod
    def _current_time() -> datetime:
        return utc_now_for_db()

    @staticmethod
    def _require_person(person_id: int) -> Person:
        return require_record(
            Person,
            Person.id == person_id,
            error_code="person_not_found",
            error_message="Person not found",
            error_details={"person_id": person_id},
        )

    @staticmethod
    def _video_count_field():
        return fn.COUNT(VideoItemPerson.id)

    @classmethod
    def _build_sort(cls, sort: str | None):
        normalized = (sort or "video_count:desc").strip().lower()
        try:
            field_name, direction = normalized.split(":", 1)
        except ValueError as exc:
            raise ApiError(422, "invalid_person_filter", "Invalid person sort", {"sort": sort}) from exc
        if field_name not in _PERSON_SORT_FIELDS or direction not in ("asc", "desc"):
            raise ApiError(422, "invalid_person_filter", "Invalid person sort", {"sort": sort})
        if field_name == "video_count":
            sort_field = cls._video_count_field()
        elif field_name == "name":
            sort_field = Person.name
        else:
            sort_field = Person.created_at
        ordered = sort_field.asc() if direction == "asc" else sort_field.desc()
        tie_breaker = Person.id.asc() if direction == "asc" else Person.id.desc()
        return [ordered, tie_breaker]

    @classmethod
    def _person_query(cls, query: str | None = None):
        video_count = cls._video_count_field().alias("video_count")
        person_query = (
            Person.select(Person, video_count)
            .join(VideoItemPerson, JOIN.LEFT_OUTER)
            .group_by(Person.id)
        )
        if query is not None:
            normalized = query.strip()
            if not normalized:
                raise ApiError(422, "invalid_person_filter", "Invalid person filter", {"query": query})
            person_query = person_query.where(Person.name.contains(normalized))
        return person_query

    @classmethod
    def _to_resource(cls, person: Person) -> PersonResource:
        return PersonResource(
            id=person.id,
            name=person.name,
            avatar_image=ImageResource.from_attributes_model(person.avatar_image)
            if person.avatar_image_id is not None
            else None,
            gender=person.gender,
            video_count=getattr(person, "video_count", 0) or 0,
            created_at=person.created_at,
            updated_at=person.updated_at,
        )

    @classmethod
    def list_persons(
        cls,
        *,
        query: str | None = None,
        sort: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PageResponse[PersonResource]:
        validate_page(page, page_size, error_code="invalid_person_filter")
        order_by = cls._build_sort(sort)
        base_query = cls._person_query(query)
        total = Person.select()
        if query is not None:
            total = total.where(Person.name.contains(query.strip()))
        total_count = total.count()
        persons = list(
            base_query.order_by(*order_by)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return PageResponse[PersonResource](
            items=[cls._to_resource(person) for person in persons],
            page=page,
            page_size=page_size,
            total=total_count,
        )

    @classmethod
    def get_person(cls, person_id: int) -> PersonResource:
        person = cls._person_query().where(Person.id == person_id).first()
        if person is None:
            raise ApiError(404, "person_not_found", "Person not found", {"person_id": person_id})
        return cls._to_resource(person)

    @classmethod
    def create_person(cls, payload: PersonCreateRequest) -> PersonResource:
        person = Person.create(name=payload.name, gender=payload.gender)
        return cls.get_person(person.id)

    @classmethod
    def update_person(cls, person_id: int, payload: PersonUpdateRequest) -> PersonResource:
        person = cls._require_person(person_id)
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if not update_data:
            raise ApiError(422, "validation_error", "At least one field must be provided")
        if "name" in update_data and update_data["name"] is not None:
            person.name = update_data["name"]
        if "gender" in update_data and update_data["gender"] is not None:
            person.gender = update_data["gender"]
        person.updated_at = cls._current_time()
        person.save()
        return cls.get_person(person.id)

    @classmethod
    def delete_person(cls, person_id: int) -> None:
        person = cls._require_person(person_id)
        # 依赖 DB 外键 CASCADE 自动清理 VideoItemPerson 关联。
        person.delete_instance(recursive=True)
