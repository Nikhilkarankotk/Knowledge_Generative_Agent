"""Equivalent of ``DocumentChunk.java``.

The Java entity stored the embedding vector as a list of floats in a dedicated
``document_chunk_embeddings`` element-collection table. The live PostgreSQL schema
instead exposes a pgvector ``vector`` column on ``document_chunk``; ``EmbeddingType``
binds to pgvector on PostgreSQL and to JSON on SQLite (tests).
"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, BigIntIdentity
from app.models.types import EmbeddingType


class DocumentChunk(Base):
    __tablename__ = "document_chunk"

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True, autoincrement=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType, nullable=True)
