"""Persisted original bytes for uploaded documents (Phase 4 export).

``document`` records *metadata* only (what the UI lists). Without the original
bytes the source-aware export can only regenerate a document from retrieved
chunks. Storing the uploaded bytes here (kept session-scoped, removed with the
session) lets the export service hand back the true original document as a
``NATIVE_FILE`` artifact, which the export specification prefers.

Columns are kept deliberately small; PostgreSQL maps :class:`LargeBinary` to
``BYTEA`` and SQLite to ``BLOB``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntIdentity


class DocumentFile(Base):
    __tablename__ = "document_file"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
