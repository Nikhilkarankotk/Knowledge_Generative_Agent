"""Custom SQLAlchemy column types shared across models."""

from __future__ import annotations

from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator


class EmbeddingType(TypeDecorator):
    """Stores float vectors as a pgvector ``vector`` column on PostgreSQL.

    The Java application persisted embeddings into a dedicated element-collection
    table, but the live PostgreSQL schema (created by the original RAG setup) uses a
    pgvector ``vector`` column on ``document_chunk``. On PostgreSQL we therefore bind
    to ``pgvector.sqlalchemy.Vector``; on other dialects (e.g. SQLite used by the test
    suite) we fall back to ``JSON`` which round-trips a list of floats.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):  # type: ignore[no-untyped-def]
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector())
        return dialect.type_descriptor(JSON())
