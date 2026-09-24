"""启动期表维护（autovacuum 调参 + 删列后 compaction）回归。"""

from __future__ import annotations

from src.model import SchemaMigration
from src.start.maintenance import (
    compact_movie_if_needed,
    ensure_autovacuum_settings,
    run_startup_maintenance,
)
from src.start.migrations.runner import DROP_MOVIE_EXTRA_MIGRATION_NAME
from tests.conftest import TEST_MODELS

_COMPACTION_MARKER = f"{DROP_MOVIE_EXTRA_MIGRATION_NAME}:compacted"


def _reloptions(clean_db, table_name: str) -> dict[str, str]:
    row = clean_db.execute_sql(
        "SELECT reloptions FROM pg_class WHERE oid = %s::regclass", (table_name,)
    ).fetchone()
    if not row or not row[0]:
        return {}
    options: dict[str, str] = {}
    for item in row[0]:
        key, _, value = item.partition("=")
        options[key] = value
    return options


def _ledger_entries(clean_db) -> set[str]:
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        return {item.name for item in SchemaMigration.select(SchemaMigration.name)}


def _record_drop_movie_extra(clean_db) -> None:
    with clean_db.bind_ctx([SchemaMigration], bind_refs=False, bind_backrefs=False):
        SchemaMigration.create(name=DROP_MOVIE_EXTRA_MIGRATION_NAME)


def _spy_vacuum(monkeypatch, clean_db) -> list[str]:
    calls: list[str] = []
    original_execute_sql = clean_db.execute_sql

    def spy_execute_sql(sql, *args, **kwargs):
        if "VACUUM" in sql:
            calls.append(sql)
        return original_execute_sql(sql, *args, **kwargs)

    monkeypatch.setattr(clean_db, "execute_sql", spy_execute_sql)
    return calls


def test_ensure_autovacuum_settings_applies_and_is_idempotent(clean_db):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    ensure_autovacuum_settings(clean_db)

    movie_options = _reloptions(clean_db, "movie")
    assert movie_options["autovacuum_vacuum_scale_factor"] == "0.02"
    assert movie_options["autovacuum_analyze_scale_factor"] == "0.01"
    assert movie_options["autovacuum_vacuum_threshold"] == "500"
    assert (
        _reloptions(clean_db, "media_thumbnail")["autovacuum_vacuum_scale_factor"]
        == "0.02"
    )

    # 再跑一次结果不变（幂等，已一致时不产生写入）。
    ensure_autovacuum_settings(clean_db)
    assert _reloptions(clean_db, "movie") == movie_options


def test_compact_movie_requires_drop_migration_record(clean_db, monkeypatch):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)

    vacuum_calls = _spy_vacuum(monkeypatch, clean_db)
    compact_movie_if_needed(clean_db)

    # 新装库或尚未执行删列迁移的库不重写。
    assert vacuum_calls == []
    assert _COMPACTION_MARKER not in _ledger_entries(clean_db)


def test_compact_movie_runs_once_and_records_marker(clean_db, monkeypatch):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    _record_drop_movie_extra(clean_db)

    vacuum_calls = _spy_vacuum(monkeypatch, clean_db)
    compact_movie_if_needed(clean_db)
    compact_movie_if_needed(clean_db)

    assert len(vacuum_calls) == 1
    assert _COMPACTION_MARKER in _ledger_entries(clean_db)


def test_run_startup_maintenance_retries_compaction_after_failure(clean_db, monkeypatch):
    clean_db.bind(TEST_MODELS, bind_refs=False, bind_backrefs=False)
    clean_db.create_tables(TEST_MODELS)
    _record_drop_movie_extra(clean_db)

    original_execute_sql = clean_db.execute_sql
    fail_state = {"fail": True}

    def flaky_execute_sql(sql, *args, **kwargs):
        if "VACUUM" in sql and fail_state["fail"]:
            raise RuntimeError("disk full")
        return original_execute_sql(sql, *args, **kwargs)

    monkeypatch.setattr(clean_db, "execute_sql", flaky_execute_sql)

    # 首次重写失败：只告警不阻断，也不落完成标记。
    run_startup_maintenance(clean_db)
    assert _COMPACTION_MARKER not in _ledger_entries(clean_db)
    assert _reloptions(clean_db, "movie")["autovacuum_vacuum_scale_factor"] == "0.02"

    # 下次启动自动重试并成功。
    fail_state["fail"] = False
    run_startup_maintenance(clean_db)
    assert _COMPACTION_MARKER in _ledger_entries(clean_db)
