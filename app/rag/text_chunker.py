"""Equivalent of ``TextChunker.java``.

Splits text into fixed-size character chunks (no overlap), exactly like the Java
``substring`` loop with ``chunkSize`` = 500.
"""

from __future__ import annotations

from app.core.exceptions import IllegalArgumentException


class TextChunker:
    def chunk_text(self, text: str, chunk_size: int) -> list[str]:
        if chunk_size <= 0:
            raise IllegalArgumentException("Chunk size must be positive")
        chunks: list[str] = []
        length = len(text)
        for start in range(0, length, chunk_size):
            chunks.append(text[start : min(length, start + chunk_size)])
        return chunks
