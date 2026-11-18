from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.actors import (
    ActorCreateRequest,
    ActorDetailResource,
    ActorFilterOptionsResource,
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorMergeRequest,
    ActorResource,
    ActorUpdateRequest,
    GfriendsActorCandidateResource,
    GfriendsActorImportRequest,
    YearResource,
)
from src.schema.catalog.movies import TagListItemResource
from src.schema.common.pagination import PageResponse
from src.service.catalog import ActorMergeService, ActorService

router = APIRouter(
    prefix="/actors",
    tags=["actors"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


def _parse_cups(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    cups = [item.strip().upper() for item in raw.split(",")]
    if not cups or any(
        not cup or not cup.isalpha() or not cup.isascii() for cup in cups
    ):
        raise ApiError(422, "invalid_actor_filter", "Invalid cup filter", {"cups": raw})
    return sorted(set(cups))


@router.get(
    "", response_model=PageResponse[ActorResource], response_model_by_alias=False
)
def list_actors(
    gender: ActorListGender = ActorListGender.ALL,
    subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
    age_min: int | None = Query(default=None, ge=0),
    age_max: int | None = Query(default=None, ge=0),
    height_min: int | None = Query(default=None, ge=1),
    height_max: int | None = Query(default=None, ge=1),
    cups: str | None = None,
    has_playable_movies: bool = False,
    sort: str | None = None,
    query: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    return ActorService.list_actors(
        gender=gender,
        subscription_status=subscription_status,
        age_min=age_min,
        age_max=age_max,
        height_min=height_min,
        height_max=height_max,
        cups=_parse_cups(cups),
        has_playable_movies=has_playable_movies,
        sort=sort,
        query=query,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=ActorDetailResource,
    response_model_by_alias=False,
    status_code=status.HTTP_201_CREATED,
)
def create_local_actor(payload: ActorCreateRequest):
    """创建本地女优。"""
    return ActorService.create_local_actor(payload)


@router.get(
    "/filter-options",
    response_model=ActorFilterOptionsResource,
    response_model_by_alias=False,
)
def get_actor_filter_options(
    gender: ActorListGender = ActorListGender.ALL,
    subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
):
    return ActorService.get_filter_options(
        gender=gender, subscription_status=subscription_status
    )


@router.get(
    "/gfriends/search",
    response_model=list[GfriendsActorCandidateResource],
)
def search_gfriends_actors(
    query: str = Query(min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=50),
):
    return ActorService.search_gfriends(query, limit=limit)


@router.post(
    "/gfriends/import",
    response_model=ActorDetailResource,
    response_model_by_alias=False,
)
def import_gfriends_actor(payload: GfriendsActorImportRequest):
    return ActorService.import_gfriends_candidate(payload)


@router.get(
    "/{actor_id}", response_model=ActorDetailResource, response_model_by_alias=False
)
def get_actor(actor_id: int):
    return ActorService.get_actor_detail(actor_id)


@router.delete("/{actor_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_local_actor(actor_id: int):
    """删除用户自建女优；仍被影片引用或作为合并目标时拒绝。"""
    ActorService.delete_local_actor(actor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/{actor_id}", response_model=ActorDetailResource, response_model_by_alias=False
)
def update_actor(actor_id: int, payload: ActorUpdateRequest):
    return ActorService.update_profile(actor_id, payload)


@router.post(
    "/{actor_id}/merge",
    response_model=ActorDetailResource,
    response_model_by_alias=False,
)
def merge_actor(actor_id: int, payload: ActorMergeRequest):
    return ActorMergeService.merge_actors(actor_id, payload.source_actor_ids)


@router.put(
    "/{actor_id}/profile-image",
    response_model=ActorDetailResource,
    response_model_by_alias=False,
)
async def upload_actor_profile_image(
    actor_id: int,
    file: UploadFile = File(...),
):
    content = await file.read()
    return ActorService.upload_profile_image(
        actor_id,
        content=content,
        content_type=file.content_type,
    )


@router.delete(
    "/{actor_id}/profile-image",
    response_model=ActorDetailResource,
    response_model_by_alias=False,
)
def clear_actor_profile_image(actor_id: int):
    return ActorService.clear_profile_image(actor_id)


@router.put("/{actor_id}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def subscribe_actor(actor_id: int):
    ActorService.set_subscription(actor_id, True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{actor_id}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe_actor(actor_id: int):
    ActorService.set_subscription(actor_id, False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{actor_id}/movie-ids", response_model=list[int], response_model_by_alias=False
)
def get_actor_movie_ids(actor_id: int):
    return ActorService.get_actor_movie_ids(actor_id)


@router.get(
    "/{actor_id}/tags",
    response_model=list[TagListItemResource],
    response_model_by_alias=False,
)
def get_actor_tags(actor_id: int):
    return ActorService.get_actor_tags(actor_id)


@router.get(
    "/{actor_id}/years",
    response_model=list[YearResource],
    response_model_by_alias=False,
)
def get_actor_years(actor_id: int):
    return ActorService.get_actor_years(actor_id)
