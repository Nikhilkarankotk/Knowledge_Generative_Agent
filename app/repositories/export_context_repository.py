"""Repository for the ``export_context`` / ``export_context_item`` tables."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import ExportContext, ExportContextItem


class ExportContextRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, context: ExportContext) -> ExportContext:
        self._session.add(context)
        self._session.flush()
        return context

    def find_by_id(self, context_id: int) -> ExportContext | None:
        return self._session.get(ExportContext, context_id)

    def find_by_chat_message_id(self, chat_message_id: int) -> ExportContext | None:
        stmt = select(ExportContext).where(ExportContext.chat_message_id == chat_message_id)
        return self._session.scalar(stmt)

    def find_by_session_id(self, session_id: str) -> list[ExportContext]:
        stmt = (
            select(ExportContext)
            .where(ExportContext.session_id == session_id)
            .order_by(ExportContext.id.asc())
        )
        return list(self._session.scalars(stmt).all())

    def save_item(self, item: ExportContextItem) -> ExportContextItem:
        self._session.add(item)
        self._session.flush()
        return item

    def save_items(self, items: list[ExportContextItem]) -> list[ExportContextItem]:
        for item in items:
            self._session.add(item)
        self._session.flush()
        return items

    def find_items(
        self, context_id: int, *, export_only: bool = True
    ) -> list[ExportContextItem]:
        stmt = (
            select(ExportContextItem)
            .where(ExportContextItem.export_context_id == context_id)
            .order_by(
                ExportContextItem.retrieval_rank.asc(),
                ExportContextItem.id.asc(),
            )
        )
        if export_only:
            stmt = stmt.where(ExportContextItem.exportable.is_(True))
        return list(self._session.scalars(stmt).all())

    def find_items_by_chat_message_id(
        self, chat_message_id: int
    ) -> list[ExportContextItem]:
        stmt = (
            select(ExportContextItem)
            .join(ExportContext, ExportContext.id == ExportContextItem.export_context_id)
            .where(ExportContext.chat_message_id == chat_message_id)
            .order_by(
                ExportContextItem.retrieval_rank.asc(),
                ExportContextItem.id.asc(),
            )
        )
        return list(self._session.scalars(stmt).all())

    def delete_by_session_id(self, session_id: str) -> None:
        # Remove child rows first (no FK enforcement on all dialects).
        context_ids = select(ExportContext.id).where(
            ExportContext.session_id == session_id
        )
        self._session.execute(
            delete(ExportContextItem).where(
                ExportContextItem.export_context_id.in_(context_ids)
            )
        )
        self._session.execute(
            delete(ExportContext).where(ExportContext.session_id == session_id)
        )

    @staticmethod
    def create_context(chat_message_id: int, session_id: str) -> ExportContext:
        return ExportContext(
            session_id=session_id,
            chat_message_id=chat_message_id,
            created_at=datetime.now(),
            status="ready",
        )
