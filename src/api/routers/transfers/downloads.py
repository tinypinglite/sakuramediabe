from typing import List

from fastapi import APIRouter, Depends, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.api.routers.deps import db_deps, get_current_user
from src.schema.transfers.downloads import (
    DownloadCandidateResource,
    DownloadCandidatesQuery,
    DownloadClientCreateRequest,
    DownloadClientResource,
    DownloadClientUpdateRequest,
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
)
from src.service.transfers import (
    DownloadClientService,
    DownloadRequestService,
    DownloadSearchService,
)

router = APIRouter(
    tags=["downloads"],
    dependencies=[Depends(db_deps)],
)


@router.get("/download-clients", response_model=List[DownloadClientResource])
def list_download_clients(current_user=Depends(get_current_user)):
    return DownloadClientService.list_clients()


@router.post(
    "/download-clients",
    response_model=DownloadClientResource,
    status_code=status.HTTP_201_CREATED,
)
def create_download_client(
    payload: DownloadClientCreateRequest,
    current_user=Depends(get_current_user),
):
    return DownloadClientService.create_client(payload)


@router.patch("/download-clients/{client_id}", response_model=DownloadClientResource)
def update_download_client(
    client_id: int,
    payload: DownloadClientUpdateRequest,
    current_user=Depends(get_current_user),
):
    return DownloadClientService.update_client(client_id, payload)


@router.delete("/download-clients/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_download_client(client_id: int, current_user=Depends(get_current_user)):
    DownloadClientService.delete_client(client_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/download-candidates", response_model=List[DownloadCandidateResource])
def list_download_candidates(
    query: DownloadCandidatesQuery = Depends(),
    current_user=Depends(get_current_user),
):
    return DownloadSearchService().search_candidates(
        movie_number=query.movie_number,
        indexer_kind=query.indexer_kind,
    )


@router.post("/download-requests", response_model=DownloadRequestCreateResponse)
def create_download_request(
    payload: DownloadRequestCreateRequest,
    current_user=Depends(get_current_user),
):
    result = DownloadRequestService().create_request(payload)
    status_code = status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    return JSONResponse(status_code=status_code, content=jsonable_encoder(result))
