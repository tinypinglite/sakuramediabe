from fastapi import APIRouter, Depends

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.metadata_provider_license import (
    MetadataProviderLicenseActivateRequest,
    MetadataProviderLicenseConnectivityTestResource,
    MetadataProviderLicenseStatusResource,
)
from src.service.system.metadata_provider_license_service import (
    MetadataProviderLicenseService,
)

router = APIRouter(
    prefix="/metadata-provider-license",
    tags=["metadata-provider-license"],
    dependencies=[Depends(db_deps)],
)


@router.get("/status", response_model=MetadataProviderLicenseStatusResource)
def get_metadata_provider_license_status(current_user=Depends(get_current_user)):
    return MetadataProviderLicenseService.get_status()


@router.get(
    "/connectivity-test",
    response_model=MetadataProviderLicenseConnectivityTestResource,
)
def test_metadata_provider_license_connectivity(current_user=Depends(get_current_user)):
    return MetadataProviderLicenseService.test_connectivity()


@router.post("/activate", response_model=MetadataProviderLicenseStatusResource)
def activate_metadata_provider_license(
    payload: MetadataProviderLicenseActivateRequest,
    current_user=Depends(get_current_user),
):
    return MetadataProviderLicenseService.activate(payload)


@router.post("/renew", response_model=MetadataProviderLicenseStatusResource)
def renew_metadata_provider_license(current_user=Depends(get_current_user)):
    return MetadataProviderLicenseService.renew()
