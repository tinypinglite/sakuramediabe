import bcrypt

from src.api.exception.errors import ApiError
from src.i18n.locale import normalize_language
from src.model import User, UserRefreshToken
from src.schema.system.account import AccountResource, AccountUpdateRequest


class AccountService:
    @staticmethod
    def get_account(user: User) -> AccountResource:
        return AccountResource.from_attributes_model(user)

    @staticmethod
    def update_account(user: User, payload: AccountUpdateRequest) -> AccountResource:
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        if "username" in update_data:
            username = update_data["username"]
            existing_user = User.get_or_none(User.username == username)
            if existing_user is not None and existing_user.id != user.id:
                raise ApiError(409, "username_conflict", "Username already exists")
            user.username = username

        if "language" in update_data:
            raw_language = update_data["language"]
            language = normalize_language(raw_language)
            if language is None:
                raise ApiError(
                    422,
                    "invalid_language",
                    "语言不受支持",
                    params={"language": raw_language},
                )
            user.language = language

        user.save()
        return AccountService.get_account(user)

    @staticmethod
    def change_password(user: User, current_password: str, new_password: str) -> None:
        if not bcrypt.checkpw(
            current_password.encode("utf-8"), user.password_hash.encode("utf-8")
        ):
            raise ApiError(401, "invalid_credentials", "Current password is incorrect")

        user.password_hash = bcrypt.hashpw(
            new_password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")
        user.save()
        UserRefreshToken.delete().execute()
