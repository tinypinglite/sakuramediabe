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
    # 客户端熔断状态：连续 5 次永久性错误后置 True，必须人工重新激活。
    # 前端可以根据 circuit_open=True 显示"请重新激活"红色 banner。
    circuit_open: bool = False
    consecutive_permanent_failures: int = 0
    last_failure_code: str | None = None
    last_failure_at: int | None = None


class MetadataProviderLicenseConnectivityTestResource(SchemaModel):
    ok: bool
    url: str
    proxy_enabled: bool
    elapsed_ms: int
    status_code: int | None = None
    error: str | None = None
