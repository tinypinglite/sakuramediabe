from src.service.transfers.rapid_upload.command_service import (
    MediaRapidUploadCommandService,
)
from src.service.transfers.rapid_upload.executor import MediaRapidUploadExecutor
from src.service.transfers.rapid_upload.item_executor import (
    MediaRapidUploadItemExecutor,
)
from src.service.transfers.rapid_upload.query_service import (
    MediaRapidUploadQueryService,
)
from src.service.transfers.rapid_upload.recovery_service import (
    MediaRapidUploadRecoveryService,
)
from src.service.transfers.rapid_upload.state_machine import (
    MediaRapidUploadStateMachine,
)


class MediaRapidUploadService(
    MediaRapidUploadCommandService,
    MediaRapidUploadQueryService,
    MediaRapidUploadRecoveryService,
    MediaRapidUploadExecutor,
    MediaRapidUploadItemExecutor,
    MediaRapidUploadStateMachine,
):
    """秒传兼容门面；内部按 command/query/executor/recovery 分工。"""
