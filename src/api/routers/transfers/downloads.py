from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadCandidateResource,
    DownloadCandidatesQuery,
    DownloadClientCreateRequest,
    DownloadClientResource,
    DownloadClientSyncResponse,
    DownloadClientUpdateRequest,
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskImportResponse,
    DownloadTaskResource,
)
from src.service.transfers import (
    DownloadClientService,
    DownloadRequestService,
    DownloadSearchService,
    DownloadTaskService,
    DownloadSyncService,
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


@router.post(
    "/download-clients/{client_id}/sync",
    response_model=DownloadClientSyncResponse,
)
def sync_download_client(client_id: int, current_user=Depends(get_current_user)):
    return DownloadSyncService().sync_client(client_id)


@router.get("/download-tasks", response_model=PageResponse[DownloadTaskResource])
def list_download_tasks(
    page: int = Query(default=1),
    page_size: int = Query(default=20),
    client_id: Optional[int] = Query(default=None),
    download_state: Optional[str] = Query(default=None),
    import_status: Optional[str] = Query(default=None),
    movie_number: Optional[str] = Query(default=None),
    query: Optional[str] = Query(default=None),
    sort: Optional[str] = Query(default=None),
    current_user=Depends(get_current_user),
):
    return DownloadTaskService.list_tasks(
        page=page,
        page_size=page_size,
        client_id=client_id,
        download_state=download_state,
        import_status=import_status,
        movie_number=movie_number,
        query=query,
        sort=sort,
    )


@router.post(
    "/download-tasks/{task_id}/import",
    response_model=DownloadTaskImportResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_download_task_import(task_id: int, current_user=Depends(get_current_user)):
    return DownloadTaskService.trigger_import(task_id)


@router.delete("/download-tasks", status_code=status.HTTP_204_NO_CONTENT)
def delete_download_tasks(
    task_ids: Optional[str] = Query(default=None),
    current_user=Depends(get_current_user),
):
    DownloadTaskService.delete_tasks(task_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
