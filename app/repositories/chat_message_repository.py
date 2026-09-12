"""Equivalent of ``ChatMessageRepository.java``.

The custom JPQL query used by ``findRecentChatSessions`` is reproduced with the
equivalent SQLAlchemy SELECT:

.. code-block:: sql

    SELECT * FROM chat_message c
    WHERE c.id IN (SELECT MIN(m.id) FROM chat_message m WHERE m.role = 'user' GROUP BY m.session_id)
    ORDER BY c.timestamp DESC
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import ChatMessage


class ChatMessageRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, message: ChatMessage) -> ChatMessage:
        self._session.add(message)
        self._session.flush()
        return message

    def find_by_id(self, message_id: int) -> ChatMessage | None:
        return self._session.get(ChatMessage, message_id)

    def find_by_session_id(self, session_id: str) -> list[ChatMessage]:
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.asc())
        )
        return list(self._session.scalars(stmt).all())

    def find_recent_chat_sessions(self) -> list[ChatMessage]:
        min_ids = (
            select(func.min(ChatMessage.id).label("min_id"))
            .where(ChatMessage.role == "user")
            .group_by(ChatMessage.session_id)
            .subquery()
        )
        stmt = (
            select(ChatMessage)
            .where(ChatMessage.id.in_(select(min_ids.c.min_id)))
            .order_by(ChatMessage.timestamp.desc())
        )
        return list(self._session.scalars(stmt).all())

    def delete_by_session_id(self, session_id: str) -> None:
        self._session.execute(
            delete(ChatMessage).where(ChatMessage.session_id == session_id)
        )
