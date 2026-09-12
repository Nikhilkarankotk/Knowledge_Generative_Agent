"""Equivalent of ``EmbeddingService.java``.

Uses the Mistral ``mistral-embed`` model via ``POST /embeddings`` and returns the
``data[0].embedding`` vector as a list of floats.
"""

from __future__ import annotations

from app.llm import MistralClient


class EmbeddingService:
    def __init__(self, client: MistralClient) -> None:
        self._client = client

    @classmethod
    def from_client(cls, client: MistralClient) -> EmbeddingService:
        return cls(client)

    def generate_embedding(self, text: str) -> list[float]:
        """Equivalent of ``generateEmbedding(text).block()``."""
        return self._client.generate_embedding(text)
