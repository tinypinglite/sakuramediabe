from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.auth import TokenCreateRequest, TokenRefreshRequest, TokenResource
from src.service.system.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(db_deps)])


@router.post(
    "/tokens",
    response_model=TokenResource,
    status_code=status.HTTP_201_CREATED,
)
def create_access_token(payload: TokenCreateRequest, request: Request):
    return AuthService.create_token_pair(
        username=payload.username,
        password=payload.password,
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post(
    "/token-refreshes",
    response_model=TokenResource,
    status_code=status.HTTP_201_CREATED,
)
def refresh_access_token(
    payload: TokenRefreshRequest,
    request: Request,
    current_user=Depends(get_current_user),
):
    return AuthService.refresh_token_pair(
        refresh_token=payload.refresh_token,
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.post("/docs-token", include_in_schema=False)
def docs_login(form_data: OAuth2PasswordRequestForm = Depends()):
    token_resource = AuthService.create_token_pair(
        username=form_data.username,
        password=form_data.password,
    )
    return {
        "access_token": token_resource.access_token,
        "token_type": "bearer",
    }
