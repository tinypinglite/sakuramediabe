from peewee import CharField

from src.model.base import BaseModel
from src.model.enums import RefreshTokenStatus
from src.model.mixins import TimestampedMixin


class DummyModel(TimestampedMixin, BaseModel):
    name = CharField()


def test_refresh_token_enums_use_documented_values():
    assert RefreshTokenStatus.REVOKED.value == "revoked"


def test_timestamp_fields_exist():
    field_names = set(DummyModel._meta.fields)

    assert "created_at" in field_names
    assert "updated_at" in field_names
