from fastapi import APIRouter, Depends, status

from src.api.routers.deps import db_deps, get_current_user
from src.schema.system.batch_operations import (
    BatchOperationAcceptedResponse,
    BatchOperationRequest,
)
from src.service.system.batch_operation_service import BatchOperationTaskService

router = APIRouter(
    tags=["batch-operations"],
    dependencies=[Depends(db_deps), Depends(get_current_user)],
)


@router.post(
    "/batch-operations",
    response_model=BatchOperationAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_batch_operation(payload: BatchOperationRequest):
    return BatchOperationTaskService.enqueue(payload)
