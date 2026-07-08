from src.schema.common.base import SchemaModel


class MovieDescTranslationSettingsTestRequest(SchemaModel):
    # 全部字段可选：不传则回退到当前保存配置。用于草稿字段覆盖式的连通性探测，不落盘。
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None
    connect_timeout_seconds: float | None = None
    text: str | None = None


class MovieDescTranslationSettingsTestResource(SchemaModel):
    ok: bool
