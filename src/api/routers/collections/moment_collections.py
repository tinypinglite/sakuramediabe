from fastapi import APIRouter, Depends, Response, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.collections.moments import (
    MomentCollectionCreateRequest,
    MomentCollectionPointItemResource,
    MomentCollectionResource,
    MomentCollectionSetPointsRequest,
    MomentCollectionUpdateRequest,
)
from src.schema.common.pagination import PageResponse
from src.service.collections import MomentCollectionService

router = APIRouter(
    prefix="/moment-collections",
    tags=["moment-collections"],
    dependencies=[Depends(db_deps)],
)


@router.get("", response_model=list[MomentCollectionResource])
def list_moment_collections(current_user=Depends(get_current_user)):
    return MomentCollectionService.list_collections()


@router.post(
    "", response_model=MomentCollectionResource, status_code=status.HTTP_201_CREATED
)
def create_moment_collection(
    payload: MomentCollectionCreateRequest, current_user=Depends(get_current_user)
):
    return MomentCollectionService.create_collection(payload)


@router.get("/{collection_id}", response_model=MomentCollectionResource)
def get_moment_collection(collection_id: int, current_user=Depends(get_current_user)):
    return MomentCollectionService.get_collection(collection_id)


@router.patch("/{collection_id}", response_model=MomentCollectionResource)
def update_moment_collection(
    collection_id: int,
    payload: MomentCollectionUpdateRequest,
    current_user=Depends(get_current_user),
):
    return MomentCollectionService.update_collection(collection_id, payload)


@router.delete("/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_moment_collection(
    collection_id: int, current_user=Depends(get_current_user)
):
    MomentCollectionService.delete_collection(collection_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{collection_id}/points",
    response_model=PageResponse[MomentCollectionPointItemResource],
)
def list_moment_collection_points(
    collection_id: int,
    page: int = 1,
    page_size: int = 20,
    current_user=Depends(get_current_user),
):
    return MomentCollectionService.list_collection_points(
        collection_id, page, page_size
    )


@router.put(
    "/{collection_id}/points/{point_id}", status_code=status.HTTP_204_NO_CONTENT
)
def add_point_to_moment_collection(
    collection_id: int,
    point_id: int,
    current_user=Depends(get_current_user),
):
    MomentCollectionService.add_point(collection_id, point_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{collection_id}/points/{point_id}", status_code=status.HTTP_204_NO_CONTENT
)
def remove_point_from_moment_collection(
    collection_id: int,
    point_id: int,
    current_user=Depends(get_current_user),
):
    MomentCollectionService.remove_point(collection_id, point_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{collection_id}/points", status_code=status.HTTP_204_NO_CONTENT)
def set_moment_collection_points(
    collection_id: int,
    payload: MomentCollectionSetPointsRequest,
    current_user=Depends(get_current_user),
):
    MomentCollectionService.set_points(collection_id, payload.point_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
