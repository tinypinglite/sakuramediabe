import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Tuple

from jose import JWTError, jwt
from passlib.context import CryptContext

from src.api.exception.errors import ApiError
from src.common.runtime_time import parse_external_datetime, to_db_utc_naive, utc_now_for_db
from src.config.config import settings
from src.model import User, UserRefreshToken
from src.model.enums import RefreshTokenStatus
from src.schema.system.auth import AuthUserSummary, TokenResource

PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")

class AuthService:
    @staticmethod
    def create_token_pair(
        username: str,
        password: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResource:
        user = User.get_or_none(User.username == username)
        if user is None or not PASSWORD_CONTEXT.verify(password, user.password_hash):
            raise ApiError(401, "invalid_credentials", "Username or password is incorrect")

        access_expires_at = AuthService._utcnow() + timedelta(
            minutes=settings.auth.access_token_expire_minutes
        )
        refresh_expires_at = AuthService._utcnow() + timedelta(
            minutes=settings.auth.refresh_token_expire_minutes
        )
        access_token = AuthService._create_access_token(user, access_expires_at)
        refresh_token, token_id, token_hash = AuthService._create_refresh_token()
        UserRefreshToken.create(
            token_id=token_id,
            token_hash=token_hash,
            status=RefreshTokenStatus.ACTIVE.value,
            expires_at=AuthService._to_db_datetime(refresh_expires_at),
            client_ip=client_ip,
            user_agent=user_agent,
        )
        user.last_login_at = AuthService._utcnow_naive()
        user.save()
        return AuthService._build_token_resource(
            user,
            access_token,
            refresh_token,
            access_expires_at,
            refresh_expires_at,
        )

    @staticmethod
    def refresh_token_pair(
        refresh_token: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> TokenResource:
        token_hash = AuthService._hash_token(refresh_token)
        token_record = UserRefreshToken.get_or_none(
            (UserRefreshToken.token_hash == token_hash)
            & (UserRefreshToken.status == RefreshTokenStatus.ACTIVE.value)
        )
        if token_record is None:
            raise ApiError(401, "invalid_refresh_token", "Refresh token is invalid")
        expires_at = AuthService._coerce_datetime(token_record.expires_at)
        if expires_at < AuthService._utcnow_naive():
            raise ApiError(401, "invalid_refresh_token", "Refresh token is invalid")

        user = User.select().order_by(User.id).first()
        if user is None:
            raise ApiError(401, "unauthorized", "Invalid access token")

        access_expires_at = AuthService._utcnow() + timedelta(
            minutes=settings.auth.access_token_expire_minutes
        )
        refresh_expires_at = AuthService._utcnow() + timedelta(
            minutes=settings.auth.refresh_token_expire_minutes
        )
        access_token = AuthService._create_access_token(user, access_expires_at)
        new_refresh_token, token_id, new_token_hash = AuthService._create_refresh_token()

        token_record.status = RefreshTokenStatus.REVOKED.value
        token_record.revoked_at = AuthService._utcnow_naive()
        token_record.replaced_by_token_id = token_id
        token_record.save()

        UserRefreshToken.create(
            token_id=token_id,
            token_hash=new_token_hash,
            status=RefreshTokenStatus.ACTIVE.value,
            expires_at=AuthService._to_db_datetime(refresh_expires_at),
            client_ip=client_ip,
            user_agent=user_agent,
        )
        return AuthService._build_token_resource(
            user,
            access_token,
            new_refresh_token,
            access_expires_at,
            refresh_expires_at,
        )

    @staticmethod
    def get_current_user(access_token: str) -> User:
        try:
            payload = jwt.decode(
                access_token,
                settings.auth.secret_key,
                algorithms=[settings.auth.algorithm],
            )
        except JWTError as exc:
            raise ApiError(401, "unauthorized", "Invalid access token") from exc

        if payload.get("type") != "access":
            raise ApiError(401, "unauthorized", "Invalid access token")

        user = User.get_or_none(User.id == int(payload["sub"]))
        if user is None:
            raise ApiError(401, "unauthorized", "Invalid access token")
        return user

    @staticmethod
    def _create_access_token(user: User, expires_at: datetime) -> str:
        payload = {
            "sub": str(user.id),
            "type": "access",
            "exp": int(expires_at.timestamp()),
        }
        return jwt.encode(
            payload,
            settings.auth.secret_key,
            algorithm=settings.auth.algorithm,
        )

    @staticmethod
    def _create_refresh_token() -> Tuple[str, str, str]:
        plain_token = secrets.token_urlsafe(32)
        token_id = secrets.token_hex(16)
        token_hash = AuthService._hash_token(plain_token)
        return plain_token, token_id, token_hash

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_token_resource(
        user: User,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
    ) -> TokenResource:
        return TokenResource(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_in=settings.auth.access_token_expire_minutes * 60,
            expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
            user=AuthUserSummary.from_attributes_model(user),
        )

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _utcnow_naive() -> datetime:
        return utc_now_for_db()

    @staticmethod
    def _to_db_datetime(value: datetime) -> datetime:
        return to_db_utc_naive(value)

    @staticmethod
    def _coerce_datetime(value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            return AuthService._to_db_datetime(value)
        parsed = parse_external_datetime(value)
        if parsed is None:
            raise ValueError("datetime value cannot be empty")
        return parsed
