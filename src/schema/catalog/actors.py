from datetime import datetime
from enum import Enum

from pydantic import Field, field_validator

from src.common import build_signed_image_url
from src.schema.common.base import SchemaModel


class ActorListGender(str, Enum):
    ALL = "all"
    FEMALE = "female"
    MALE = "male"


class ActorListSubscriptionStatus(str, Enum):
    ALL = "all"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"


ACTOR_LIST_SORT_FIELDS = (
    "subscribed_at",
    "name",
    "movie_count",
)


class ImageResource(SchemaModel):
    id: int
    origin: str
    small: str
    medium: str
    large: str

    @staticmethod
    def _sign_image_path(value: str) -> str:
        if value.startswith("/files/images/"):
            return value
        return build_signed_image_url(value)

    @field_validator("origin", "small", "medium", "large")
    @classmethod
    def sign_image_path(cls, value: str) -> str:
        if not value:
            return value
        return cls._sign_image_path(value)


class ActorResource(SchemaModel):
    id: int
    javdb_id: str
    name: str
    alias_name: str
    profile_image: ImageResource | None = None
    is_subscribed: bool
    subscribed_at: datetime | None = None
    movie_count: int = 0


class ActorDetailResource(ActorResource):
    pass


class MovieIdResource(SchemaModel):
    movie_id: int


class YearResource(SchemaModel):
    year: int


class ActorJavdbSearchRequest(SchemaModel):
    actor_name: str = Field(min_length=1)

    @field_validator("actor_name")
    @classmethod
    def validate_actor_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("actor_name cannot be blank")
        return normalized
