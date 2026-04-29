"""闭源 metadata provider 授权运行时参数。"""

from dataclasses import dataclass
import os

from src.config.config import settings


BACKEND_VERSION_ENV_KEY = "SAKURAMEDIA_BACKEND_VERSION"
BACKEND_VERSION_DEFAULT = "v0.0.1"
METADATA_LICENSE_STATE_PATH = "/data/config/provider-license-state.json"


@dataclass(frozen=True)
class MetadataProviderLicenseRuntime:
    version: str
    state_path: str
    license_proxy: str | None

    def as_provider_kwargs(self) -> dict[str, str | None]:
        # 闭源 provider 工厂统一使用这组授权参数。
        return {
            "version": self.version,
            "state_path": self.state_path,
            "license_proxy": self.license_proxy,
        }


def resolve_metadata_provider_license_runtime() -> MetadataProviderLicenseRuntime:
    backend_version = (os.getenv(BACKEND_VERSION_ENV_KEY) or "").strip() or BACKEND_VERSION_DEFAULT
    return MetadataProviderLicenseRuntime(
        version=backend_version,
        state_path=METADATA_LICENSE_STATE_PATH,
        license_proxy=settings.metadata.normalized_license_proxy,
    )
