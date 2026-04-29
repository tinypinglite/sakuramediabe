from unittest.mock import Mock

from src.common.database import ensure_database_ready
from src.config.config import settings


def test_ensure_database_ready_reuses_initialized_open_database(monkeypatch):
    fake_database = Mock()
    fake_database.is_closed.return_value = False
    get_database = Mock(return_value=fake_database)
    init_database = Mock()

    monkeypatch.setattr("src.common.database.get_database", get_database)
    monkeypatch.setattr("src.common.database.init_database", init_database)

    resolved_database = ensure_database_ready()

    assert resolved_database is fake_database
    get_database.assert_called_once_with()
    init_database.assert_not_called()
    fake_database.connect.assert_not_called()


def test_ensure_database_ready_initializes_proxy_when_needed(monkeypatch):
    fake_database = Mock()
    fake_database.is_closed.return_value = False
    init_database = Mock(return_value=fake_database)

    monkeypatch.setattr(
        "src.common.database.get_database",
        Mock(side_effect=RuntimeError("Database has not been initialized")),
    )
    monkeypatch.setattr("src.common.database.init_database", init_database)

    resolved_database = ensure_database_ready()

    assert resolved_database is fake_database
    init_database.assert_called_once_with(settings.database)
    fake_database.connect.assert_not_called()


def test_ensure_database_ready_connects_closed_database(monkeypatch):
    fake_database = Mock()
    fake_database.is_closed.return_value = True

    monkeypatch.setattr("src.common.database.get_database", Mock(return_value=fake_database))
    monkeypatch.setattr("src.common.database.init_database", Mock())

    resolved_database = ensure_database_ready()

    assert resolved_database is fake_database
    fake_database.connect.assert_called_once_with()
