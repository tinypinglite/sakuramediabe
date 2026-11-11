from contextlib import contextmanager

from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.model import BackgroundTaskRun, SystemNotification
from src.scheduler.contracts import JobDefinition
from src.service.system.activity.notifications import NotificationService
from src.service.system.activity.task_catalog import TASK_NAME_REGISTRY
from src.service.system.activity.task_text import (
    resolve_job_cli_help,
    resolve_job_task_name,
    resolve_task_name,
)


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def _host_job() -> JobDefinition:
    return JobDefinition(
        task_key="movie_heat_update",
        log_name="movie_heat_update",
        cli_name="movie_heat_update",
        cli_help="影片热度更新",
        cron_setting="movie_heat",
        handler=lambda reporter, params: None,
    )


def _plugin_job() -> JobDefinition:
    return JobDefinition(
        task_key="plugin_sync",
        log_name="plugin_sync",
        cli_name="plugin_sync",
        cli_help="插件同步",
        cli_help_i18n={"en": "Plugin sync", "zh-CN": "插件同步"},
        default_cron="0 0 * * *",
        handler=lambda reporter, params: None,
        plugin_id="demo",
    )


def test_resolve_task_name_translates_host_task():
    with _language("en"):
        assert resolve_task_name("movie_heat_update") == "Movie heat update"
        assert (
            resolve_task_name("movie_heat_update", "影片热度更新")
            == "Movie heat update"
        )


def test_every_registered_task_has_english_name():
    with _language("en"):
        for task_key, zh_name in TASK_NAME_REGISTRY.items():
            assert resolve_task_name(task_key) != zh_name, task_key


def test_resolve_task_name_preserves_dynamic_name():
    with _language("en"):
        assert (
            resolve_task_name("movie_heat_update", "动态导入 #42")
            == "动态导入 #42"
        )


def test_resolve_task_name_falls_back_to_registry_for_unknown_key():
    with _language("en"):
        assert resolve_task_name("unknown_task") == "unknown_task"


def test_plugin_job_text_uses_cli_help_i18n():
    with _language("en"):
        assert resolve_job_task_name(_plugin_job()) == "Plugin sync"
        assert resolve_job_cli_help(_plugin_job()) == "Plugin sync"
    with _language("zh-CN"):
        assert resolve_job_task_name(_plugin_job()) == "插件同步"


def test_failed_task_notification_is_written_in_english(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="movie_heat_update",
        task_name="影片热度更新",
        trigger_type="scheduled",
        state="failed",
        error_message="内部错误",
        result_summary={},
    )

    with _language("en"):
        NotificationService.notify_task_result(task_run, failed=True)

    notification = SystemNotification.get(SystemNotification.dedupe_key == f"task_run_result:{task_run.id}")
    assert notification.title == "Movie heat update failed"
    assert notification.content == "Background task failed"


def test_failed_task_notification_keeps_chinese_behavior(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="movie_heat_update",
        task_name="影片热度更新",
        trigger_type="scheduled",
        state="failed",
        error_message="内部错误",
        result_summary={},
    )

    with _language("zh-CN"):
        NotificationService.notify_task_result(task_run, failed=True)

    notification = SystemNotification.get(SystemNotification.dedupe_key == f"task_run_result:{task_run.id}")
    assert notification.title == "影片热度更新执行失败"
    assert notification.content == "内部错误"


def test_completed_with_failures_notification_is_written_in_english(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="movie_heat_update",
        task_name="影片热度更新",
        trigger_type="scheduled",
        state="completed",
        result_text="failed=1",
        result_summary={"failed": 1},
    )

    with _language("en"):
        NotificationService.notify_task_result(task_run, failed=False)

    notification = SystemNotification.get(SystemNotification.dedupe_key == f"task_run_result:{task_run.id}")
    assert notification.title == "Movie heat update completed"
    assert notification.content == "Background task completed"
