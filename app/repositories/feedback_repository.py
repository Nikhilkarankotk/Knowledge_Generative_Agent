"""Equivalent of ``FeedbackRepository.java``."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Feedback


class FeedbackRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, feedback: Feedback) -> Feedback:
        self._session.add(feedback)
        self._session.flush()
        return feedback

    def find_by_message_id(self, message_id: int) -> Feedback | None:
        stmt = select(Feedback).where(Feedback.message_id == message_id)
        return self._session.scalar(stmt)
