from src.schema.common.base import SchemaModel


class MetadataProviderLicenseActivateRequest(SchemaModel):
    activation_code: str


class MetadataProviderLicenseStatusResource(SchemaModel):
    configured: bool
    active: bool
    instance_id: str | None = None
    expires_at: int | None = None
    license_valid_until: int | None = None
    renew_after_seconds: int | None = None
    error_code: str | None = None
    message: str | None = None


class MetadataProviderLicenseConnectivityTestResource(SchemaModel):
    ok: bool
    url: str
    proxy_enabled: bool
    elapsed_ms: int
    status_code: int | None = None
    error: str | None = None
