from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from src.api.exception.errors import ApiError
from src.config.config import settings
from src.model import get_database, init_database
from src.service.system.auth_service import AuthService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/docs-token", auto_error=False)


def db_deps():
    try:
        database = get_database()
    except RuntimeError:
        database = init_database(settings.database)
    if database.is_closed():
        database.connect()
    return database


def get_current_user(
    access_token: str | None = Depends(oauth2_scheme),
):
    if access_token is None:
        raise ApiError(401, "unauthorized", "Authentication required")
    return AuthService.get_current_user(access_token)
