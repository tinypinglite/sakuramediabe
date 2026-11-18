import hashlib
import secrets
from datetime import timedelta

from src.api.exception.errors import ApiError
from src.common.runtime_time import utc_now_for_db
from src.model import ApiKey, User
from src.schema.system.api_key import ApiKeyCreatedResource, ApiKeyResource

# Bearer 头里以该前缀开头的 token 按 API key 校验（JWT 为 eyJ... 三段式）。
API_KEY_PREFIX = "sk-"
_KEY_HINT_LENGTH = 11  # "sk-" + 8 位随机字符
_LAST_USED_REFRESH_INTERVAL = timedelta(minutes=5)


class ApiKeyService:
    @staticmethod
    def list_api_keys() -> list[ApiKeyResource]:
        rows = ApiKey.select().order_by(ApiKey.created_at.desc(), ApiKey.id.desc())
        return [ApiKeyResource.from_attributes_model(row) for row in rows]

    @staticmethod
    def create_api_key(name: str) -> ApiKeyCreatedResource:
        raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
        row = ApiKey.create(
            name=name.strip(),
            key_hint=raw_key[:_KEY_HINT_LENGTH],
            key_hash=_hash_key(raw_key),
        )
        resource = ApiKeyResource.from_attributes_model(row)
        return ApiKeyCreatedResource(**resource.model_dump(), key=raw_key)

    @staticmethod
    def delete_api_key(key_id: int) -> None:
        deleted = ApiKey.delete().where(ApiKey.id == key_id).execute()
        if not deleted:
            raise ApiError(404, "api_key_not_found", "API key not found")

    @staticmethod
    def authenticate(raw_key: str) -> User:
        """校验 API key 并返回单账号用户；last_used_at 做节流更新。"""
        row = ApiKey.get_or_none(ApiKey.key_hash == _hash_key(raw_key))
        if row is None:
            raise ApiError(401, "unauthorized", "Invalid access token")

        now = utc_now_for_db()
        if (
            row.last_used_at is None
            or now - row.last_used_at > _LAST_USED_REFRESH_INTERVAL
        ):
            row.last_used_at = now
            row.save(only=[ApiKey.last_used_at])

        user = User.select().order_by(User.id).first()
        if user is None:
            raise ApiError(401, "unauthorized", "Invalid access token")
        return user


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
