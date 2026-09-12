"""Response schema for a persisted attached document (``Document`` entity)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_serializer

from app.models import Document


class DocumentOut(BaseModel):
    id: int | None = None
    sessionId: str | None = None
    filename: str | None = None
    sizeBytes: int | None = None
    contentType: str | None = None
    status: str | None = None
    uploadedAt: datetime | None = None

    @field_serializer("uploadedAt")
    def _serialize_uploaded_at(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        iso = value.isoformat()
        if "." in iso:
            iso = iso.split(".")[0]
        return iso

    @classmethod
    def from_model(cls, document: Document) -> DocumentOut:
        return cls(
            id=document.id,
            sessionId=document.session_id,
            filename=document.filename,
            sizeBytes=document.size_bytes,
            contentType=document.content_type,
            status=document.status,
            uploadedAt=document.uploaded_at,
        )
