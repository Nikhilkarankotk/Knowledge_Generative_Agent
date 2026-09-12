"""Repository for the ``document`` table (attached-document metadata)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Document


class DocumentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, document: Document) -> Document:
        self._session.add(document)
        self._session.flush()
        return document

    def find_by_id(self, document_id: int) -> Document | None:
        return self._session.get(Document, document_id)

    def find_by_session_id(self, session_id: str) -> list[Document]:
        stmt = (
            select(Document)
            .where(Document.session_id == session_id)
            .order_by(Document.id.asc())
        )
        return list(self._session.scalars(stmt).all())

    def find_by_session_and_filename(
        self, session_id: str, filename: str
    ) -> Document | None:
        stmt = select(Document).where(
            Document.session_id == session_id, Document.filename == filename
        )
        return self._session.scalar(stmt)

    def upsert(
        self,
        session_id: str | None,
        filename: str | None,
        size_bytes: int | None,
        content_type: str | None,
        status: str | None,
        uploaded_at: datetime | None,
    ) -> Document:
        """Update the metadata row for (session, filename) or insert a new one."""
        existing: Document | None = None
        if session_id and filename:
            existing = self.find_by_session_and_filename(session_id, filename)
        if existing is not None:
            existing.size_bytes = size_bytes
            existing.content_type = content_type
            existing.status = status
            existing.uploaded_at = uploaded_at
            return existing
        return self.save(
            Document(
                session_id=session_id,
                filename=filename,
                size_bytes=size_bytes,
                content_type=content_type,
                status=status,
                uploaded_at=uploaded_at,
            )
        )

    def delete_by_id(self, document_id: int) -> None:
        self._session.execute(delete(Document).where(Document.id == document_id))

    def delete_by_session_id(self, session_id: str) -> None:
        self._session.execute(delete(Document).where(Document.session_id == session_id))
