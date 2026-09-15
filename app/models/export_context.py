"""Export context persistence for the source-aware export (Phase 4).

Two concerns are recorded when a chat turn ends:

* :class:`ExportContext` - one row per assistant ``chat_message`` that produced a
  retrievable answer. It links the export to the message that is the source of
  truth (``chat_message.id``), the session it belongs to, and the resolved export
  outcome (scenario strategy, selected format, reason) once an export runs.
* :class:`ExportContextItem` - one row per *actual retrieval artifact* the agent
  used in that turn (a RAG source file, a Confluence page, a GitHub repository,
  or a SharePoint document). Items are captured from the retrieval context - the
  identifiers/metadata the plugins actually retrieved - not parsed out of the AI
  answer. ``content_reference`` holds a JSON snapshot of the retrieved content so
  a generated export can be produced even if the live source is temporarily
  unavailable. ``metadata`` is a free-form JSON bag of source-specific details
  (e.g. repo default branch, drive/document ids, mime type, modified date).

Source types use the canonical ``UPLOADED_DOCUMENT`` / ``CONFLUENCE`` / ``GITHUB``
/ ``SHAREPOINT`` constants (see ``app/export/formats.py``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntIdentity


class ExportContext(Base):
    __tablename__ = "export_context"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    chat_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Resolved export outcome (filled when an export runs).
    strategy: Mapped[str | None] = mapped_column(String(50), nullable=True)
    requested_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    format_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ExportContextItem(Base):
    __tablename__ = "export_context_item"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True, autoincrement=True)
    export_context_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    source_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Stable, displayable identifier for the artifact (filename, page id, repo id...).
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form JSON bag of source-specific retrieval details. Column name is
    # ``metadata``; the attribute is ``meta`` because ``metadata`` is reserved by
    # SQLAlchemy's declarative API.
    meta: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    retrieval_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retrieval_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # JSON snapshot of the retrieved content used to build generated exports.
    content_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    export_strategy: Mapped[str | None] = mapped_column(String(50), nullable=True)
    native_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exportable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=True)
