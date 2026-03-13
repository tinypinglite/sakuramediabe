from datetime import datetime

from peewee import DateTimeField

from src.model.base import BaseModel


class TimestampedMixin(BaseModel):
    created_at = DateTimeField(default=datetime.utcnow)
    updated_at = DateTimeField(default=datetime.utcnow)
