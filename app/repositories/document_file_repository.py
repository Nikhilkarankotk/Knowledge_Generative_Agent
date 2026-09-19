"""Repository for the ``document_file`` table (uploaded original bytes)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import DocumentFile


class DocumentFileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, document_file: DocumentFile) -> DocumentFile:
        self._session.add(document_file)
        self._session.flush()
        return document_file

    def find_by_id(self, document_file_id: int) -> DocumentFile | None:
        return self._session.get(DocumentFile, document_file_id)

    def find_by_session_and_filename(
        self, session_id: str, filename: str
    ) -> DocumentFile | None:
        stmt = select(DocumentFile).where(
            DocumentFile.session_id == session_id, DocumentFile.filename == filename
        )
        return self._session.scalar(stmt)

    def upsert(
        self,
        session_id: str | None,
        filename: str | None,
        content: bytes,
        content_type: str | None,
        uploaded_at: datetime | None = None,
    ) -> DocumentFile:
        """Replace the stored bytes for ``(session, filename)`` or insert a row."""
        existing: DocumentFile | None = None
        if session_id and filename:
            existing = self.find_by_session_and_filename(session_id, filename)
        if existing is not None:
            existing.content = content
            existing.content_type = content_type
            existing.size_bytes = len(content)
            if uploaded_at is not None:
                existing.uploaded_at = uploaded_at
            return existing
        return self.save(
            DocumentFile(
                session_id=session_id,
                filename=filename,
                content=content,
                content_type=content_type,
                size_bytes=len(content),
                uploaded_at=uploaded_at or datetime.now(),
            )
        )

    def delete_by_session_id(self, session_id: str) -> None:
        self._session.execute(
            delete(DocumentFile).where(DocumentFile.session_id == session_id)
        )

    def delete_by_session_and_filename(self, session_id: str, filename: str) -> None:
        self._session.execute(
            delete(DocumentFile).where(
                DocumentFile.session_id == session_id, DocumentFile.filename == filename
            )
        )
