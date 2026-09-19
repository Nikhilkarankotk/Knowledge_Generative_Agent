"""Tests for the startup database bootstrap (auto-create missing database/tables)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from app.core.database import Database
from app.models import Base


def _sqlite_db() -> Database:
    from app.core.config import Settings

    return Database(Settings(database_url="sqlite+pysqlite://"))


def test_init_schema_creates_tables_on_fresh_sqlite() -> None:
    db = _sqlite_db()
    Base.metadata.drop_all(db.engine)
    db.init_schema()
    try:
        db.init_schema()  # second run must be idempotent
        assert "document_chunk" in Base.metadata.tables
    finally:
        db.engine.dispose()


def test_ensure_database_exists_is_noop_on_sqlite() -> None:
    db = _sqlite_db()
    with patch("app.core.database.make_url") as make_url_mock:
        db._ensure_database_exists()
        make_url_mock.assert_not_called()
    db.engine.dispose()


def test_ensure_database_exists_creates_missing_postgres_database() -> None:
    from app.core.config import Settings

    db = Database(
        Settings(
            database_url="postgresql://postgres:postgres@localhost:5432/Knowledge_Gen_Agent"
        )
    )
    maintenance_connection = MagicMock()
    maintenance_connection.execute.return_value.scalar.return_value = None
    maintenance_engine = MagicMock()
    maintenance_engine.connect.return_value.__enter__.return_value = maintenance_connection

    with patch("app.core.database.create_engine", return_value=maintenance_engine) as create_engine_mock:
        db._ensure_database_exists()

    create_engine_mock.assert_called_once()
    assert str(create_engine_mock.call_args.args[0]).endswith("/postgres")
    maintenance_connection.execute.assert_called()
    sql = maintenance_connection.execute.call_args_list[1].args[0].text
    assert 'CREATE DATABASE "Knowledge_Gen_Agent"' in sql
    maintenance_engine.dispose.assert_called_once()


def test_ensure_database_exists_skips_existing_database() -> None:
    from app.core.config import Settings

    db = Database(
        Settings(
            database_url="postgresql://postgres:postgres@localhost:5432/Knowledge_Gen_Agent"
        )
    )
    maintenance_connection = MagicMock()
    maintenance_connection.execute.return_value.scalar.return_value = 1
    maintenance_engine = MagicMock()
    maintenance_engine.connect.return_value.__enter__.return_value = maintenance_connection

    with patch("app.core.database.create_engine", return_value=maintenance_engine):
        db._ensure_database_exists()

    assert maintenance_connection.execute.call_count == 1
    assert "CREATE DATABASE" not in maintenance_connection.execute.call_args_list[0].args[0].text
    maintenance_engine.dispose.assert_called_once()


def test_ensure_database_exists_survives_bootstrap_failure(caplog) -> None:
    from app.core.config import Settings

    caplog.set_level(logging.WARNING)
    db = Database(
        Settings(
            database_url="postgresql://postgres:postgres@localhost:5432/Knowledge_Gen_Agent"
        )
    )
    with patch(
        "app.core.database.create_engine",
        side_effect=RuntimeError("could not connect to maintenance db"),
    ) as create_engine_mock:
        db._ensure_database_exists()  # must not raise

    create_engine_mock.assert_called_once()
    assert "Could not automatically create database" in caplog.text
