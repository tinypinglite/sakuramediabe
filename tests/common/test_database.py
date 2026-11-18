"""数据库连接自恢复（ReconnectMixin）的回归测试。"""

from __future__ import annotations

import psycopg2
import pytest
from peewee import InterfaceError


def _terminate_backend(database) -> None:
    backend_pid = database.execute_sql("SELECT pg_backend_pid()").fetchone()[0]
    killer = psycopg2.connect(dbname=database.database, **database.connect_params)
    killer.autocommit = True
    try:
        killer.cursor().execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
    finally:
        killer.close()


def test_query_reconnects_after_server_terminates_connection(test_db):
    database = test_db
    database.execute_sql("SELECT 1")
    old_connection = database.connection()

    _terminate_backend(database)

    assert database.execute_sql("SELECT 1").fetchone() == (1,)
    assert database.connection() is not old_connection


def test_query_reconnects_after_local_connection_close(test_db):
    database = test_db
    database.execute_sql("SELECT 1")
    old_connection = database.connection()
    old_connection.close()

    assert database.execute_sql("SELECT 1").fetchone() == (1,)
    assert database.connection() is not old_connection


def test_connection_loss_inside_transaction_is_not_retried(test_db):
    database = test_db
    database.execute_sql("SELECT 1")

    with (
        pytest.raises(InterfaceError),
        database.atomic(),
    ):
        database.connection().close()
        database.execute_sql("SELECT 1")

    assert not database.in_transaction()
    assert database.execute_sql("SELECT 1").fetchone() == (1,)


def test_healthy_connection_is_reused(test_db):
    database = test_db
    database.execute_sql("SELECT 1")
    connection = database.connection()

    database.execute_sql("SELECT 2")

    assert database.connection() is connection
