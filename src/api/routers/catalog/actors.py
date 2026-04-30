import json
from typing import List

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import StreamingResponse

from src.api.routers.deps import db_deps, get_current_user
from src.schema.catalog.actors import (
    ActorDetailResource,
    ActorJavdbSearchRequest,
    ActorListGender,
    ActorListSubscriptionStatus,
    ActorResource,
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


def _to_sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get("", response_model=PageResponse[ActorResource], response_model_by_alias=False)
def list_actors(
    gender: ActorListGender = ActorListGender.ALL,
    subscription_status: ActorListSubscriptionStatus = ActorListSubscriptionStatus.ALL,
    sort: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    return ActorService.list_actors(
        gender=gender,
        subscription_status=subscription_status,
        sort=sort,
        page=page,
        page_size=page_size,
    )


@router.post("/search/javdb/stream")
def search_javdb_actor_stream(
    payload: ActorJavdbSearchRequest,
):
    def stream():
        for event, event_payload in ActorService.stream_search_and_upsert_actor_from_javdb(
            payload.actor_name
        ):
            yield _to_sse_event(event, event_payload)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{actor_id}", response_model=ActorDetailResource, response_model_by_alias=False)
def get_actor(actor_id: int):
    return ActorService.get_actor_detail(actor_id)


@router.put("/{actor_id}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def subscribe_actor(actor_id: int):
    ActorService.set_subscription(actor_id, True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{actor_id}/subscription", status_code=status.HTTP_204_NO_CONTENT)
def unsubscribe_actor(actor_id: int):
    ActorService.set_subscription(actor_id, False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{actor_id}/movie-ids", response_model=List[int], response_model_by_alias=False)
def get_actor_movie_ids(actor_id: int):
    return ActorService.get_actor_movie_ids(actor_id)


@router.get("/{actor_id}/tags", response_model=List[TagResource], response_model_by_alias=False)
def get_actor_tags(actor_id: int):
    return ActorService.get_actor_tags(actor_id)


@router.get("/{actor_id}/years", response_model=List[YearResource], response_model_by_alias=False)
def get_actor_years(actor_id: int):
    return ActorService.get_actor_years(actor_id)
