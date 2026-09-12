"""Equivalent of ``Feedback.java``."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, BigIntIdentity


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True, autoincrement=True)
    message_id: Mapped[int | None] = mapped_column(
        BigIntIdentity, ForeignKey("chat_message.id"), nullable=True
    )
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corrected_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    message = relationship("ChatMessage")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Feedback id={self.id} message_id={self.message_id} rating={self.rating}>"
