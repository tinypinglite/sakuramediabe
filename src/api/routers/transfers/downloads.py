from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.api.exception.errors import ApiError
from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadCandidateResource,
    DownloadCandidatesQuery,
    DownloadClientCreateRequest,
    DownloadClientDiagnosticResource,
    DownloadClientResource,
    DownloadClientTestRequest,
    DownloadClientUpdateRequest,
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskResource,
    DownloadTasksQuery,
)
from src.service.transfers.downloads.client_config_service import DownloadClientService
from src.service.transfers.downloads.request_service import DownloadRequestService
from src.service.transfers.downloads.search_service import DownloadSearchService
from src.service.transfers.downloads.task_service import DownloadTaskService

router = APIRouter(tags=["downloads"], dependencies=[Depends(db_deps)])


@router.get("/download-clients", response_model=list[DownloadClientResource])
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


@router.post(
    "/download-clients/test",
    response_model=DownloadClientDiagnosticResource,
)
def test_download_client(
    payload: DownloadClientTestRequest,
    current_user=Depends(get_current_user),
):
    return DownloadClientService.test_client(payload)


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


@router.get("/download-candidates", response_model=list[DownloadCandidateResource])
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


@router.get("/download-tasks", response_model=PageResponse[DownloadTaskResource])
def list_download_tasks(
    query: DownloadTasksQuery = Depends(),
    state: list[str] | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    return DownloadTaskService.list_tasks(
        page=query.page,
        page_size=query.page_size,
        client_id=query.client_id,
        movie_number=query.movie_number,
        state=state,
        sort=query.sort,
    )


@router.delete("/download-tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_download_task(
    task_id: int,
    delete_files: bool = Query(default=False),
    confirm_delete_files: bool = Query(default=False),
    current_user=Depends(get_current_user),
):
    if delete_files and not confirm_delete_files:
        raise ApiError(
            422,
            "download_task_delete_confirmation_required",
            "Deleting downloaded files requires explicit confirmation",
            {"task_id": task_id},
        )
    DownloadTaskService.delete_task(task_id, delete_files=delete_files)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
