"""Persisted metadata for documents attached to a session.

Records what has been ingested into the session's RAG knowledge base (filename, size,
content type, status) so the UI can list "Attached Documents (n)" truthfully and
independently of browser state. Chunks for a document live in ``document_chunk`` with a
matching ``source_filename``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntIdentity


class Document(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
