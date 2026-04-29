from src.schema.common.base import SchemaModel


class MovieDescTranslationSettingsResource(SchemaModel):
    enabled: bool
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float
    connect_timeout_seconds: float


class MovieDescTranslationSettingsUpdateRequest(SchemaModel):
    enabled: bool | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None
    connect_timeout_seconds: float | None = None


class MovieDescTranslationSettingsTestRequest(SchemaModel):
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None
    connect_timeout_seconds: float | None = None
    text: str | None = None


class MovieDescTranslationSettingsTestResource(SchemaModel):
    ok: bool
