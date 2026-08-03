
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.api.exception.errors import ApiError
from src.api.routers._utils import sse_streaming_response, to_sse_event
from src.api.routers.deps import db_deps, get_current_user
from src.schema.common.pagination import PageResponse
from src.schema.transfers.downloads import (
    DownloadCandidateResource,
    DownloadCandidatesQuery,
    DownloadClientCreateRequest,
    DownloadClientProbeStorageTestRequest,
    DownloadClientProbeTestRequest,
    DownloadClientResource,
    DownloadClientStorageTestResponse,
    DownloadClientTestResponse,
    DownloadClientUpdateRequest,
    DownloadRequestCreateRequest,
    DownloadRequestCreateResponse,
    DownloadTaskActionResponse,
    DownloadTaskResource,
    DownloadTasksQuery,
)
from src.service.transfers import (
    DownloadClientService,
    DownloadRequestService,
    DownloadSearchService,
    DownloadTaskService,
)

router = APIRouter(
    tags=["downloads"],
    dependencies=[Depends(db_deps)],
)


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


# 注意:probe/* 静态路径必须声明在 {client_id} 参数化路径之前,
# 否则 FastAPI 会先尝试用 "probe" 匹配 client_id: int 并返回 422。
@router.post("/download-clients/probe/test", response_model=DownloadClientTestResponse)
def probe_test_download_client(
    payload: DownloadClientProbeTestRequest,
    current_user=Depends(get_current_user),
):
    return DownloadClientService.probe_test(payload)


@router.post(
    "/download-clients/probe/storage-test",
    response_model=DownloadClientStorageTestResponse,
)
def probe_test_download_client_storage(
    payload: DownloadClientProbeStorageTestRequest,
    current_user=Depends(get_current_user),
):
    return DownloadClientService.probe_storage_test(payload)


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


@router.get("/download-clients/{client_id}/test", response_model=DownloadClientTestResponse)
def test_download_client(client_id: int, current_user=Depends(get_current_user)):
    return DownloadClientService.test_client(client_id)


@router.post("/download-clients/{client_id}/storage-test", response_model=DownloadClientStorageTestResponse)
def test_download_client_storage(client_id: int, current_user=Depends(get_current_user)):
    return DownloadClientService.test_storage(client_id)


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
    current_user=Depends(get_current_user),
):
    return DownloadTaskService.list_tasks(
        page=query.page,
        page_size=query.page_size,
        client_id=query.client_id,
        movie_number=query.movie_number,
        download_state=query.download_state,
        sort=query.sort,
    )


@router.get("/download-tasks/stream")
def stream_download_tasks(
    request: Request,
    client_id: int | None = Query(default=None, gt=0),
    movie_number: str | None = Query(default=None),
    current_user=Depends(get_current_user),
):
    hub = request.app.state.download_progress_hub
    subscription = hub.subscribe(client_id=client_id, movie_number=movie_number)

    def stream():
        for event, payload in hub.iter_events(subscription):
            yield to_sse_event(event, payload)

    return sse_streaming_response(stream())


@router.post(
    "/download-tasks/{task_id}/pause",
    response_model=DownloadTaskActionResponse,
)
def pause_download_task(task_id: int, current_user=Depends(get_current_user)):
    return DownloadTaskService.pause_task(task_id)


@router.post(
    "/download-tasks/{task_id}/resume",
    response_model=DownloadTaskActionResponse,
)
def resume_download_task(task_id: int, current_user=Depends(get_current_user)):
    return DownloadTaskService.resume_task(task_id)


@router.delete("/download-tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_download_task(
    task_id: int,
    request: Request,
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
    removed = DownloadTaskService.delete_task(task_id, delete_files=delete_files)
    request.app.state.download_progress_hub.publish_task_removed(removed)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
