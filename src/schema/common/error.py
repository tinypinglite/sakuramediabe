from src.schema.common.base import SchemaModel


class ErrorBody(SchemaModel):
    code: str
    message: str
    details: dict | None = None


class ErrorResponse(SchemaModel):
    error: ErrorBody
