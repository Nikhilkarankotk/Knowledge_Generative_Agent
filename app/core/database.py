"""Database session management.

Wraps SQLAlchemy engine/session factory created from application settings. On startup the
application mirrors ``DatabaseConfig.java`` by executing ``CREATE EXTENSION IF NOT EXISTS
vector`` (PostgreSQL) and (like Hibernate ``ddl-auto: update``) creates missing tables.
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.models import Base

logger = logging.getLogger(__name__)


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
        """Equivalent of JPA ``ddl-auto: update`` + ``DatabaseConfig``."""
        if self._settings.is_postgres:
            try:
                with self.engine.connect() as connection:
                    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
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
                    connection.commit()
            except Exception as exc:  # noqa: BLE001 - do not crash startup if extension is missing
                logger.warning("Could not run PostgreSQL schema migration: %s", exc)
        Base.metadata.create_all(bind=self.engine)

    def create_session(self) -> Session:
        return self.session_factory()
