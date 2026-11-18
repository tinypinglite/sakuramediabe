"""任务进度文案的 catalog 化：进度文本必须随语言上下文切换。

服务层只声明 ``text_key``，渲染统一发生在 ``emit_progress`` 这一个出口；
这里锁定「en 语言上下文 → 英文进度文本」的行为。
"""

import ast
from contextlib import contextmanager
from pathlib import Path

import pytest

from src.common.service_helpers import emit_progress
from src.config.config import settings
from src.i18n.locale import LanguageState, reset_language_state, set_language_state
from src.model import BackgroundTaskRun, MediaLibrary
from src.plugins.provider_protocol import ImportFile
from src.service.discovery.ranking_service import (
    RANKING_SOURCE_OWNERS,
    RANKING_SOURCES,
    RankingBoardDefinition,
    RankingSourceDefinition,
    RankingSyncService,
    register_plugin_ranking_sources,
)
from src.service.system.activity.task_execution import TaskRunReporter
from src.service.transfers.imports.import_service import MediaImportService


def _contains_cjk(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text)


def _string_literals(node: ast.AST) -> list[str]:
    """取出常量或 f-string 里的字面量片段。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
    return []


def _hardcoded_progress_texts() -> list[str]:
    """扫描服务层里写死中文的进度文案。

    进度文案只能来自 catalog：服务层写 ``text=`` 中文常量就是回归。
    """
    hits: list[str] = []
    for path in Path("src/service").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else ""
            )
            if name not in {"emit_progress", "emit"}:
                continue
            for keyword in node.keywords:
                if keyword.arg != "text":
                    continue
                literals = _string_literals(keyword.value)
                if any(_contains_cjk(text) for text in literals):
                    hits.append(f"{path}:{keyword.value.lineno}")
    return hits


@contextmanager
def _language(locale: str):
    token = set_language_state(LanguageState(header_language=locale))
    try:
        yield
    finally:
        reset_language_state(token)


@pytest.fixture
def ranking_registry():
    """排名来源注册表是进程级全局；测试结束后恢复原状。"""
    sources = dict(RANKING_SOURCES)
    owners = dict(RANKING_SOURCE_OWNERS)
    try:
        yield
    finally:
        RANKING_SOURCES.clear()
        RANKING_SOURCES.update(sources)
        RANKING_SOURCE_OWNERS.clear()
        RANKING_SOURCE_OWNERS.update(owners)


def test_emit_progress_renders_catalog_key_in_current_language(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="library_import",
        task_name="媒体库导入",
        trigger_type="scheduled",
        state="running",
        result_summary={},
    )
    reporter = TaskRunReporter(task_run_id=task_run.id)

    with _language("en"):
        emit_progress(
            reporter.progress_callback,
            text_key="tasks.library_import.progress.file_processed",
            text_params={"index": 3, "total": 7},
        )

    assert BackgroundTaskRun.get_by_id(task_run.id).progress_text == "Processed 3/7"


def test_reporter_keeps_plugin_free_text_untouched(test_db):
    task_run = BackgroundTaskRun.create(
        task_key="library_import",
        task_name="媒体库导入",
        trigger_type="scheduled",
        state="running",
        result_summary={},
    )
    reporter = TaskRunReporter(task_run_id=task_run.id)

    with _language("en"):
        reporter.emit(text="plugin supplied text")

    assert BackgroundTaskRun.get_by_id(task_run.id).progress_text == "plugin supplied text"


def test_ranking_sync_progress_is_translated_to_english(
    test_db, monkeypatch, ranking_registry
):
    register_plugin_ranking_sources(
        [
            RankingSourceDefinition(
                key="probe_source",
                name="探测来源",
                name_i18n={"en": "Probe Source"},
                boards=(
                    RankingBoardDefinition(
                        key="probe_board",
                        name="探测榜单",
                        name_i18n={"en": "Probe Board"},
                        supported_periods=("2026",),
                    ),
                ),
            )
        ],
        {"probe_source": "probe_plugin"},
    )
    monkeypatch.setattr(
        RankingSyncService,
        "sync_board_period",
        lambda self, **kwargs: {
            "fetched_numbers": 0,
            "imported_movies": 0,
            "local_hit_movies": 0,
            "skipped_movies": 0,
            "stored_items": 0,
        },
    )
    payloads: list[dict] = []
    with _language("en"):
        RankingSyncService().sync_all_rankings(progress_callback=payloads.append)

    assert payloads
    assert payloads[0]["text"] == "Starting ranking sync"
    assert payloads[-1]["text"] == "Synced Probe Source-Probe Board-2026"
    assert all(payload["text"] for payload in payloads)


def test_import_progress_is_translated_to_english(test_db, monkeypatch):
    class ScanProvider:
        def scan_import_source(self, *, source_ref):
            return (
                ImportFile(
                    source_ref={"id": "clip"},
                    name="clip.mp4",
                    relative_path="clip.mp4",
                    size_bytes=4096,
                    is_video=True,
                ),
            )

    monkeypatch.setattr(settings.media, "allowed_min_video_file_size", 100)
    library = MediaLibrary.create(
        name="Library",
        provider_key="test",
        provider_config={},
    )
    service = MediaImportService(
        provider=ScanProvider(),
        catalog_import_service=object(),
    )
    payloads: list[dict] = []

    with _language("en"):
        service.import_from_source(
            {"source": "probe"},
            library.id,
            progress_callback=payloads.append,
        )

    texts = [payload["text"] for payload in payloads]
    assert "Media provider scan completed" in texts
    assert "Processed 1/1" in texts
    assert all(payload.get("text") for payload in payloads)


def test_service_progress_text_has_no_hardcoded_chinese():
    assert _hardcoded_progress_texts() == []
