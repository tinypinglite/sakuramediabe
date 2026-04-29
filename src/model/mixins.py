from peewee import DateTimeField

from src.common.runtime_time import utc_now_for_db
from src.model.base import BaseModel


class TimestampedMixin(BaseModel):
    created_at = DateTimeField(default=utc_now_for_db)
    updated_at = DateTimeField(default=utc_now_for_db)
