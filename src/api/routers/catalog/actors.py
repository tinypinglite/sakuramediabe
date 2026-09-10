from fastapi import APIRouter, Depends, File, Query, Response, UploadFile, status

from src.api.exception.errors import ApiError
from src.api.routers._utils import sse_streaming_response, to_sse_event
from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.actors import (
    ActorDetailResource,
    ActorFilterOptionsResource,
    ActorJavdbSearchRequest,
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorResource,
    ActorUpdateRequest,
    YearResource,
)
from src.schema.catalog.movies import TagResource
from src.schema.common.pagination import PageResponse
from src.service.catalog import ActorService

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
    sort: str | None = None,
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
        sort=sort,
        page=page,
        page_size=page_size,
    )


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


@router.post("/search/javdb/stream")
def search_javdb_actor_stream(
    payload: ActorJavdbSearchRequest,
):
    def stream():
        for (
            event,
            event_payload,
        ) in ActorService.stream_search_and_upsert_actor_from_javdb(payload.actor_name):
            yield to_sse_event(event, event_payload)

    return sse_streaming_response(stream())


@router.get(
    "/{actor_id}", response_model=ActorDetailResource, response_model_by_alias=False
)
def get_actor(actor_id: int):
    return ActorService.get_actor_detail(actor_id)


@router.patch(
    "/{actor_id}", response_model=ActorDetailResource, response_model_by_alias=False
)
def update_actor(actor_id: int, payload: ActorUpdateRequest):
    return ActorService.update_profile(actor_id, payload)


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
    "/{actor_id}/tags", response_model=list[TagResource], response_model_by_alias=False
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
