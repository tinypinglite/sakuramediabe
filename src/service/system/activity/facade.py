"""Activity service 的统一公开入口。"""

from src.service.system.activity.bootstrap import ActivityBootstrapService
from src.service.system.activity.notifications import NotificationService
from src.service.system.activity.task_execution import TaskExecutionService
from src.service.system.activity.task_runs import TaskRunService


class ActivityService(
    TaskExecutionService,
    TaskRunService,
    NotificationService,
    ActivityBootstrapService,
):
    pass
