from .contracts import JobDefinition
from .logging import get_task_logger

__all__ = ["JOB_REGISTRY", "JobDefinition", "get_task_logger"]


def __getattr__(name: str):
    # JOB_REGISTRY 延迟导入，避免插件契约加载 scheduler.contracts 时触发注册表循环导入。
    if name == "JOB_REGISTRY":
        from .registry import JOB_REGISTRY

        return JOB_REGISTRY
    raise AttributeError(name)
