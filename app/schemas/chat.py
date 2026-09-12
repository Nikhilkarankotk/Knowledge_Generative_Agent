"""Equivalent of ``ChatRequest.java`` and the serialized ``ChatMessage`` entity.

The response uses the exact JSON field names produced by Jackson for the Java
``ChatMessage`` entity: ``id``, ``sessionId``, ``role``, ``content``,
``detectedLanguage``, ``isTranslated``, ``timestamp``.
"""

from datetime import datetime

from pydantic import BaseModel, field_serializer

from app.models import ChatMessage


class ChatRequest(BaseModel):
    message: str


class ChatMessageOut(BaseModel):
    """Response body for a ``ChatMessage`` (matches Jackson field naming)."""

    id: int | None = None
    sessionId: str | None = None
    role: str | None = None
    content: str | None = None
    detectedLanguage: str | None = None
    isTranslated: bool | None = None
    timestamp: datetime | None = None

    @field_serializer("timestamp")
    def _serialize_timestamp(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        # Jackson serializes LocalDateTime as ISO-8601 without microseconds.
        iso = value.isoformat()
        if "." in iso:
            iso = iso.split(".")[0]
        return iso

    @classmethod
    def from_message(cls, message: ChatMessage) -> "ChatMessageOut":
        return cls(
            id=message.id,
            sessionId=message.session_id,
            role=message.role,
            content=message.content,
            detectedLanguage=message.detected_language,
            isTranslated=message.is_translated,
            timestamp=message.timestamp,
        )
