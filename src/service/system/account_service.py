from passlib.context import CryptContext

from src.api.exception.errors import ApiError
from src.model import User, UserRefreshToken
from src.schema.system.account import AccountResource, AccountUpdateRequest

PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AccountService:
    @staticmethod
    def get_account(user: User) -> AccountResource:
        return AccountResource.from_attributes_model(user)

    @staticmethod
    def update_account(user: User, payload: AccountUpdateRequest) -> AccountResource:
        update_data = payload.model_dump(exclude_unset=True, by_alias=False)
        username = update_data["username"]
        existing_user = User.get_or_none(User.username == username)
        if existing_user is not None and existing_user.id != user.id:
            raise ApiError(409, "username_conflict", "Username already exists")

        for field_name, value in update_data.items():
            setattr(user, field_name, value)
        user.save()
        return AccountService.get_account(user)

    @staticmethod
    def change_password(user: User, current_password: str, new_password: str) -> None:
        if not PASSWORD_CONTEXT.verify(current_password, user.password_hash):
            raise ApiError(401, "invalid_credentials", "Current password is incorrect")

        user.password_hash = PASSWORD_CONTEXT.hash(new_password)
        user.save()
        UserRefreshToken.delete().execute()
