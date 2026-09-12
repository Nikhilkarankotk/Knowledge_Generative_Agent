"""Equivalent of ``DocumentChunkRepository.java``."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import DocumentChunk


class DocumentChunkRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def begin_nested(self):
        """Start a SAVEPOINT so a failed replace can be rolled back independently."""
        return self._session.begin_nested()

    def save(self, chunk: DocumentChunk) -> DocumentChunk:
        self._session.add(chunk)
        self._session.flush()
        return chunk

    def find_by_session_id(self, session_id: str) -> list[DocumentChunk]:
        stmt = select(DocumentChunk).where(DocumentChunk.session_id == session_id)
        return list(self._session.scalars(stmt).all())

    def count_by_session_id(self, session_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(DocumentChunk)
            .where(DocumentChunk.session_id == session_id)
        )
        return int(self._session.scalar(stmt) or 0)

    def delete_by_session_id(self, session_id: str) -> None:
        self._session.execute(
            delete(DocumentChunk).where(DocumentChunk.session_id == session_id)
        )

    def delete_by_session_and_null_source_filename(self, session_id: str) -> None:
        """Delete only the session's legacy chunks that carry no ``source_filename``.

        Used when detaching a pre-migration document row without a filename; scoped to
        the session so other documents' chunks are never touched.
        """
        self._session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.session_id == session_id,
                DocumentChunk.source_filename.is_(None),
            )
        )

    def delete_by_session_and_source_filename(
        self, session_id: str, filename: str
    ) -> None:
        """Delete only the chunks whose ``source_filename`` exactly matches ``filename``."""
        self._session.execute(
            delete(DocumentChunk).where(
                DocumentChunk.session_id == session_id,
                DocumentChunk.source_filename == filename,
            )
        )
