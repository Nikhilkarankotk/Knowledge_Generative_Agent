"""Equivalent of ``ChatMessage.java``."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntIdentity


class ChatMessage(Base):
    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True, autoincrement=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_language: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_translated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    timestamp: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
