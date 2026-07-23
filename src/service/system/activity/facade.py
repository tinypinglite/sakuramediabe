"""Activity 兼容门面。

无状态 service 通过多继承聚合公开入口，旧调用方无需感知内部模块拆分。
"""

from src.service.system.activity.bootstrap import ActivityBootstrapService
from src.service.system.activity.context import TaskRunContextService
from src.service.system.activity.notifications import NotificationService
from src.service.system.activity.task_execution import TaskExecutionService
from src.service.system.activity.task_runs import TaskRunService


class ActivityService(
    TaskExecutionService,
    TaskRunService,
    TaskRunContextService,
    NotificationService,
    ActivityBootstrapService,
):
    pass
