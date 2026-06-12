from fastapi import APIRouter, Depends, Query, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.videos.persons import (
    PersonCreateRequest,
    PersonResource,
    PersonUpdateRequest,
)
from src.service.videos import PersonService

router = APIRouter(
    prefix="/persons",
    tags=["persons"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.get("", response_model=PageResponse[PersonResource])
def list_persons(
    query: str | None = Query(default=None),
    sort: str | None = Query(default=None),
    page: int = 1,
    page_size: int = 20,
):
    return PersonService.list_persons(query=query, sort=sort, page=page, page_size=page_size)


@router.post("", response_model=PersonResource, status_code=status.HTTP_201_CREATED)
def create_person(payload: PersonCreateRequest):
    return PersonService.create_person(payload)


@router.get("/{person_id}", response_model=PersonResource)
def get_person(person_id: int):
    return PersonService.get_person(person_id)


@router.patch("/{person_id}", response_model=PersonResource)
def update_person(person_id: int, payload: PersonUpdateRequest):
    return PersonService.update_person(person_id, payload)


@router.delete("/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person(person_id: int):
    PersonService.delete_person(person_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
