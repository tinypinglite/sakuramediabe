from fastapi import APIRouter, Depends, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.account import (
    AccountPasswordChangeRequest,
    AccountResource,
    AccountUpdateRequest,
)
from src.service.system.account_service import AccountService

router = APIRouter(prefix="/account", tags=["account"], dependencies=[Depends(db_deps)])


@router.get("", response_model=AccountResource)
def get_account(current_user=Depends(get_current_user)):
    return AccountService.get_account(current_user)


@router.patch("", response_model=AccountResource)
def update_account(payload: AccountUpdateRequest, current_user=Depends(get_current_user)):
    return AccountService.update_account(current_user, payload)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: AccountPasswordChangeRequest,
    current_user=Depends(get_current_user),
):
    AccountService.change_password(
        current_user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
