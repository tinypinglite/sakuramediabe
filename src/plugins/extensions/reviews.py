"""按番号提供电影评论的可选插件能力。"""

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.plugins.contracts import PluginExtension

REVIEWS_EXTENSION_KEY = "catalog.movie_reviews"
REVIEWS_HOST_API_VERSION = 18


class PluginMovieReview(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, revalidate_instances="always"
    )

    id: int = 0
    score: int = 0
    content: str = ""
    created_at: datetime | None = None
    username: str = ""
    like_count: int = Field(default=0, ge=0)
    watch_count: int = Field(default=0, ge=0)


class PluginMovieReviewSource(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    fetch_reviews: Callable[
        [str, int, int, str], list[PluginMovieReview] | None
    ]


def validate_reviews_extension(
    *, plugin_id: str, extension: PluginExtension
) -> PluginMovieReviewSource:
    return PluginMovieReviewSource.model_validate(extension.data)
