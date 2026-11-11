"""插件写回电影互动数时使用的公开模型。"""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PluginMovieStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float | None = Field(default=None, ge=0)
    score_number: int | None = Field(default=None, ge=0, strict=True)
    watched_count: int | None = Field(default=None, ge=0, strict=True)
    want_watch_count: int | None = Field(default=None, ge=0, strict=True)
    comment_count: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("至少提供一个互动数字段")
        return self
