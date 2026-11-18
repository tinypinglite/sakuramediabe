from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from src.api.exception.errors import ApiError
from src.common.database import ensure_database_ready
from src.i18n.locale import apply_user_language
from src.service.system.api_key_service import API_KEY_PREFIX, ApiKeyService
from src.service.system.auth_service import AuthService

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/docs-token", auto_error=False)


def db_deps():
    return ensure_database_ready()


def get_current_user(
    access_token: str | None = Depends(oauth2_scheme),
):
    if access_token is None:
        raise ApiError(401, "unauthorized", "Authentication required")
    # 外部集成的 API key（sk- 前缀）与 JWT 共用 Bearer 头，分支校验。
    if access_token.startswith(API_KEY_PREFIX):
        user = ApiKeyService.authenticate(access_token)
    else:
        user = AuthService.get_current_user(access_token)
    # 请求头优先，用户偏好作为无 header 时的回退；原地修改可变 LanguageState。
    apply_user_language(getattr(user, "language", None))
    return user
