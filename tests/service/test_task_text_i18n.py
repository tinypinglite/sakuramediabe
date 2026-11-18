from contextlib import contextmanager

from src.i18n import translate
from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.model import BackgroundTaskRun, SystemNotification
from src.scheduler.contracts import JobDefinition
from src.schema.transfers.media_import import ImportRequest
from src.service.system.activity.notifications import NotificationService
from src.service.system.activity.task_catalog import TASK_NAME_REGISTRY
from src.service.system.activity.task_text import (
    resolve_job_cli_help,
    resolve_job_task_name,
    resolve_task_name,
)
from src.service.transfers.shared.import_task_service import ImportTaskService


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


def _host_job() -> JobDefinition:
    return JobDefinition(
        task_key="movie_stats_sync",
        log_name="movie_stats_sync",
        cli_name="movie_stats_sync",
        cli_help="影片互动数与热度同步",
        cron_setting="movie_stats_sync",
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
        assert resolve_task_name("movie_stats_sync") == "Movie stats and heat sync"
        assert (
            resolve_task_name("movie_stats_sync", "影片互动数与热度同步")
            == "Movie stats and heat sync"
        )


def test_every_registered_task_has_english_name():
    with _language("en"):
        for task_key, zh_name in TASK_NAME_REGISTRY.items():
            assert resolve_task_name(task_key) != zh_name, task_key


def test_resolve_task_name_preserves_dynamic_name():
    with _language("en"):
        assert (
            resolve_task_name("movie_stats_sync", "动态导入 #42")
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


def test_import_task_name_uses_current_language():
    jav_request = ImportRequest(media_kind="jav", library_id=1, source_ref={})
    video_request = ImportRequest(media_kind="video", library_id=1, source_ref={})

    with _language("en"):
        assert ImportTaskService._task_name(jav_request) == "JAV media library import"
        assert (
            ImportTaskService._task_name(video_request)
            == "Video media library import"
        )
    with _language("zh-CN"):
        assert ImportTaskService._task_name(jav_request) == "JAV媒体库导入"
        assert ImportTaskService._task_name(video_request) == "视频媒体库导入"


def test_library_import_dynamic_titles_render_in_current_language():
    with _language("en"):
        assert (
            translate(
                "tasks.library_import.title.download_import",
                title="ABC-123",
            )
            == "Download task import: ABC-123"
        )
        assert (
            translate("tasks.library_import.title.download_reimport", count=1)
            == "Re-import 1 downloaded task"
        )
        assert (
            translate("tasks.library_import.title.download_reimport", count=3)
            == "Re-import 3 downloaded tasks"
        )
        assert (
            translate(
                "tasks.library_import.title.download_continuous_import",
                count=2,
            )
            == "Continuous import of 2 downloaded tasks"
        )
        assert (
            translate("tasks.library_import.title.failed_item_retry")
            == "Retry failed JAV import items"
        )
    with _language("zh-CN"):
        assert (
            translate(
                "tasks.library_import.title.download_import",
                title="ABC-123",
            )
            == "下载任务导入 ABC-123"
        )
        assert (
            translate("tasks.library_import.title.download_reimport", count=3)
            == "下载任务重新导入（3个）"
        )
        assert (
            translate("tasks.library_import.title.failed_item_retry")
            == "JAV失败项重试导入"
        )


def test_failed_task_notification_is_written_in_english(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="movie_stats_sync",
        task_name="影片互动数与热度同步",
        trigger_type="scheduled",
        state="failed",
        error_message="内部错误",
        result_summary={},
    )

    with _language("en"):
        NotificationService.notify_task_result(task_run, failed=True)

    notification = SystemNotification.get(SystemNotification.dedupe_key == f"task_run_result:{task_run.id}")
    assert notification.title == "Movie stats and heat sync failed"
    assert notification.content == "Background task failed"


def test_failed_task_notification_keeps_chinese_behavior(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="movie_stats_sync",
        task_name="影片互动数与热度同步",
        trigger_type="scheduled",
        state="failed",
        error_message="内部错误",
        result_summary={},
    )

    with _language("zh-CN"):
        NotificationService.notify_task_result(task_run, failed=True)

    notification = SystemNotification.get(SystemNotification.dedupe_key == f"task_run_result:{task_run.id}")
    assert notification.title == "影片互动数与热度同步执行失败"
    assert notification.content == "内部错误"


def test_completed_with_failures_notification_is_written_in_english(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="movie_stats_sync",
        task_name="影片互动数与热度同步",
        trigger_type="scheduled",
        state="completed",
        result_text="failed=1",
        result_summary={"failed": 1},
    )

    with _language("en"):
        NotificationService.notify_task_result(task_run, failed=False)

    notification = SystemNotification.get(SystemNotification.dedupe_key == f"task_run_result:{task_run.id}")
    assert notification.title == "Movie stats and heat sync completed"
    assert notification.content == "Background task completed"
