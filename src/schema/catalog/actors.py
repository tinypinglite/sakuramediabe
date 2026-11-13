from datetime import date, datetime
from enum import Enum

from pydantic import Field, field_validator, model_validator

from src.common import build_signed_image_url
from src.model.catalog.actors import ACTOR_IMAGE_FIELDS
from src.schema.common.base import SchemaModel
from src.schema.common.batch import MAX_BATCH_ITEMS


class ActorListGender(str, Enum):
    ALL = "all"
    FEMALE = "female"
    MALE = "male"


class ActorListSubscriptionStatus(str, Enum):
    ALL = "all"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"


class ImageResource(SchemaModel):
    id: int
    origin: str

    @field_validator("origin")
    @classmethod
    def sign_image_path(cls, value: str) -> str:
        if not value:
            return value
        if value.startswith("/files/images/"):
            return value
        return build_signed_image_url(value)


class ActorResource(SchemaModel):
    id: int
    name: str
    alias_name: str
    display_name: str
    profile_image: ImageResource | None = None
    is_subscribed: bool
    subscribed_at: datetime | None = None
    movie_count: int = 0
    age: int | None = None
    birthday: date | None = None
    height_cm: int | None = None
    bust_cm: int | None = None
    waist_cm: int | None = None
    hips_cm: int | None = None
    cup: str | None = None

    @classmethod
    def from_actor(cls, actor):
        return cls.model_validate(_actor_resource_payload(actor))


class ActorDetailResource(ActorResource):
    gender: int = 0
    birthplace: str | None = None
    blood_type: str | None = None
    display_name_override: str | None = None
    has_profile_image_override: bool = False
    mutation_revision: int = 0
    manual_fields: list[str] = Field(default_factory=list)


def _actor_resource_payload(actor) -> dict:
    field_owners = actor.field_owners or {}
    return {
        "id": actor.id,
        "name": actor.name,
        "alias_name": actor.alias_name,
        "display_name": actor.display_name,
        "profile_image": actor.effective_profile_image,
        "is_subscribed": actor.is_subscribed,
        "subscribed_at": actor.subscribed_at,
        "movie_count": getattr(actor, "movie_count", 0) or 0,
        "gender": actor.gender,
        "birthday": actor.birthday,
        "age": actor.age,
        "height_cm": actor.height_cm,
        "bust_cm": actor.bust_cm,
        "waist_cm": actor.waist_cm,
        "hips_cm": actor.hips_cm,
        "cup": actor.cup,
        "birthplace": actor.birthplace,
        "blood_type": actor.blood_type,
        "display_name_override": actor.display_name_override,
        "has_profile_image_override": actor.has_profile_image_override,
        "mutation_revision": actor.mutation_revision,
        # 头像覆盖的 owner 不进 manual_fields：图片状态由 has_profile_image_override 表达，
        # 这里只报人工锁定过的资料字段。
        "manual_fields": sorted(
            name
            for name, owner in field_owners.items()
            if owner == "host:manual" and name not in ACTOR_IMAGE_FIELDS
        ),
    }


class ActorCreateRequest(SchemaModel):
    """创建本地女优，默认按女优处理。"""

    name: str = Field(min_length=1, max_length=255)
    alias_name: str | None = Field(default=None, max_length=1024)
    gender: int = 1

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value):
        if not isinstance(value, str):
            return value
        return value.strip()

    @field_validator("alias_name", mode="before")
    @classmethod
    def normalize_alias_name(cls, value):
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, value):
        if value not in {0, 1, 2}:
            raise ValueError("gender 必须是 0、1 或 2")
        return value


class ActorUpdateRequest(SchemaModel):
    # 演员是本地实体，名称和别名的变更由电影导入时的名称匹配合并。
    name: str | None = Field(default=None, max_length=255)
    alias_name: str | None = Field(default=None, max_length=1024)
    display_name_override: str | None = Field(default=None, max_length=255)
    gender: int | None = None
    birthday: date | None = None
    height_cm: int | None = Field(default=None, ge=1)
    bust_cm: int | None = Field(default=None, ge=1)
    waist_cm: int | None = Field(default=None, ge=1)
    hips_cm: int | None = Field(default=None, ge=1)
    cup: str | None = None
    birthplace: str | None = Field(default=None, max_length=255)
    blood_type: str | None = Field(default=None, max_length=255)

    @field_validator(
        "name",
        "alias_name",
        "display_name_override",
        "birthplace",
        "blood_type",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value):
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None

    @field_validator("gender")
    @classmethod
    def validate_gender(cls, value):
        if value is not None and value not in {0, 1, 2}:
            raise ValueError("gender 必须是 0、1 或 2")
        return value

    @field_validator("cup", mode="before")
    @classmethod
    def normalize_cup(cls, value):
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip().upper()
        if not normalized:
            return None
        if not normalized.isascii() or not normalized.isalpha() or len(normalized) > 4:
            raise ValueError("cup 必须是 1 到 4 个英文字母")
        return normalized

    @model_validator(mode="after")
    def validate_explicit_gender(self):
        if "gender" in self.model_fields_set and self.gender is None:
            raise ValueError("gender 不能为 null，未知请使用 0")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name 不能为空")
        return self


class ActorFilterRangeResource(SchemaModel):
    min: int | None = None
    max: int | None = None
    populated_count: int = 0


class ActorCupFilterOption(SchemaModel):
    value: str
    count: int


class ActorFilterOptionsResource(SchemaModel):
    actor_count: int
    as_of_date: date
    age: ActorFilterRangeResource
    height_cm: ActorFilterRangeResource
    cups: list[ActorCupFilterOption]


class MovieIdResource(SchemaModel):
    movie_id: int


class YearResource(SchemaModel):
    year: int
    movie_count: int


class GfriendsActorCandidateResource(SchemaModel):
    candidate_id: str
    name: str
    alias_names: list[str] = Field(default_factory=list)
    avatar_url: str | None = None
    existing_actor_id: int | None = None


class GfriendsActorImportRequest(SchemaModel):
    candidate_id: str = Field(min_length=1, max_length=64)

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("candidate_id cannot be blank")
        return normalized


class ActorMergeRequest(SchemaModel):
    source_actor_ids: list[int] = Field(
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
    )

    @field_validator("source_actor_ids")
    @classmethod
    def validate_source_actor_ids(cls, value: list[int]) -> list[int]:
        if any(actor_id <= 0 for actor_id in value):
            raise ValueError("source_actor_ids 必须是正整数")
        return value
