"""Database session management.

Wraps SQLAlchemy engine/session factory created from application settings. On startup the
application mirrors ``DatabaseConfig.java`` by executing ``CREATE EXTENSION IF NOT EXISTS
vector`` (PostgreSQL) and (like Hibernate ``ddl-auto: update``) creates missing tables.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.models import Base

logger = logging.getLogger(__name__)

# PostgreSQL has no way to connect to a database that does not exist yet, so the
# auto-creation bootstrap connects to this server-level maintenance database.
_MAINTENANCE_DATABASE = "postgres"


class Database:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        engine_options: dict = {"echo": settings.database_echo_sql, "future": True}
        if settings.sqlalchemy_database_url.startswith("sqlite"):
            # In-memory SQLite needs a shared connection so every session sees the data.
            engine_options["poolclass"] = StaticPool
            engine_options["connect_args"] = {"check_same_thread": False}
        self.engine = create_engine(settings.sqlalchemy_database_url, **engine_options)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )

    def init_schema(self) -> None:
        """Equivalent of JPA ``ddl-auto: update`` + ``DatabaseConfig``.

        Any database failure (unreachable server, wrong credentials, missing
        extension, missing table for an ALTER-only migration) only logs a
        warning - startup never crashes on a misconfigured database. The
        configured database itself is created first if it does not exist
        (``_ensure_database_exists``); the migration block then runs and is
        allowed to fail (fresh database has no tables yet);
        ``create_all`` always runs afterwards to materialise any missing tables.
        """
        self._ensure_database_exists()
        if self._settings.is_postgres:
            # The pgvector extension must be committed on its own: on a fresh
            # database the migration statements below fail (tables do not exist
            # yet), which would roll back the extension DDL if they shared the
            # same transaction.
            try:
                with self.engine.begin() as connection:
                    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as exc:  # noqa: BLE001 - do not crash startup on a missing extension
                logger.warning("Could not install the pgvector extension: %s", exc)
            try:
                # Migration for databases that already have tables. This is
                # allowed to fail on a fresh database (no tables to migrate).
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE document_chunk "
                            "ADD COLUMN IF NOT EXISTS source_filename VARCHAR(255)"
                        )
                    )
                    # One-time migration: remove pre-migration orphan chunks that have
                    # no source_filename, then enforce the invariant that every chunk is
                    # tagged with its source file.
                    connection.execute(
                        text(
                            "DELETE FROM document_chunk "
                            "WHERE source_filename IS NULL"
                        )
                    )
                    connection.execute(
                        text(
                            "ALTER TABLE document_chunk "
                            "ALTER COLUMN source_filename SET NOT NULL"
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - do not crash startup on a missing table
                logger.warning("Could not run database schema migration: %s", exc)
        try:
            Base.metadata.create_all(bind=self.engine)
        except Exception as exc:  # noqa: BLE001 - do not crash startup if the database is unavailable
            logger.warning("Could not create database tables: %s", exc)

    def create_session(self) -> Session:
        return self.session_factory()

    def _ensure_database_exists(self) -> None:
        """Create the configured PostgreSQL database if it does not exist yet.

        Fresh clones connect against a non-existent database and fail before any
        table can be created. This bootstrap connects to the server-level
        ``postgres`` maintenance database, checks ``pg_database`` and issues
        ``CREATE DATABASE`` (quoting the identifier so mixed-case names like
        ``Knowledge_Gen_Agent`` survive) before ``init_schema`` runs the DDL.
        SQLite needs no bootstrap (the file/URL is created implicitly) and
        PostgreSQL extensions-versions handles the rest lazily.

        Failures (missing maintenance database, missing ``CREATEDB`` privilege)
        are logged, never fatal - ``init_schema`` can still create tables if the
        database already exists, and callers that need the database will surface
        the real connection error with a clear message.
        """
        if not self._settings.is_postgres:
            return
        url = make_url(self._settings.sqlalchemy_database_url)
        database = url.database
        if not database:
            return
        maintenance_url = url.set(database=_MAINTENANCE_DATABASE)
        try:
            maintenance_engine = create_engine(maintenance_url, isolation_level="AUTOCOMMIT")
            try:
                with maintenance_engine.connect() as connection:
                    exists = connection.execute(
                        text("SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": database},
                    ).scalar()
                    if exists:
                        logger.info("Database %r already exists", database)
                        return
                    quoted = f'"{database.replace(chr(34), chr(34) * 2)}"'
                    connection.execute(text(f"CREATE DATABASE {quoted}"))
                    logger.info("Created database %r because it did not exist", database)
            finally:
                maintenance_engine.dispose()
        except Exception as exc:  # noqa: BLE001 - never crash startup on bootstrap failure
            logger.warning(
                "Could not automatically create database %r (create it manually "
                "if the app cannot start): %s",
                database,
                exc,
            )
