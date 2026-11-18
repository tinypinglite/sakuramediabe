from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, TypeVar

from playhouse.shortcuts import model_to_dict
from pydantic import BaseModel, ConfigDict, field_serializer
from typing_extensions import Self

from src.common.runtime_time import serialize_runtime_local_value

TSchemaModel = TypeVar("TSchemaModel", bound="SchemaModel")


class SchemaModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        from_attributes=True,
    )

    @field_serializer("*", when_used="json", check_fields=False)
    def serialize_runtime_datetime(self, value):
        return serialize_runtime_local_value(value)

    @classmethod
    def from_attributes_model(cls, obj) -> Self:
        return cls.model_validate(obj, from_attributes=True)

    @classmethod
    def from_peewee_model(
        cls,
        obj,
        *,
        recurse: bool = False,
        backrefs: bool = False,
        extra: dict | None = None,
    ) -> Self:
        payload = model_to_dict(obj, recurse=recurse, backrefs=backrefs)
        if extra:
            payload.update(extra)
        return cls.model_validate(payload)

    @classmethod
    def from_items(
        cls,
        iterable: Iterable,
        *,
        mode: Literal["attributes", "peewee_dict"] = "attributes",
        recurse: bool = False,
        backrefs: bool = False,
    ) -> list[Self]:
        if mode == "peewee_dict":
            return [
                cls.from_peewee_model(item, recurse=recurse, backrefs=backrefs)
                for item in iterable
            ]
        return [cls.from_attributes_model(item) for item in iterable]
