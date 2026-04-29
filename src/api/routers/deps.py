from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from src.api.exception.errors import ApiError
from src.common.database import ensure_database_ready
from src.service.system.auth_service import AuthService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/docs-token", auto_error=False)


def db_deps():
    return ensure_database_ready()


def get_current_user(
    access_token: str | None = Depends(oauth2_scheme),
):
    if access_token is None:
        raise ApiError(401, "unauthorized", "Authentication required")
    return AuthService.get_current_user(access_token)
