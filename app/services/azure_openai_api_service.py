"""Adapter that exposes the Azure OpenAI client through the LLM service interface.

Keeps the same duck-typed ``generate_response(prompt)`` contract the rest of the
application expects (``MistralApiService`` uses the identical signature), so the
RAG answer generation can be handed either service without changing its code.
"""

from __future__ import annotations

from app.llm.azure_openai import AzureOpenAIClient


class AzureOpenAIApiService:
    """Generates chat responses via an Azure OpenAI deployment (e.g. ``luna``)."""

    def __init__(self, client: AzureOpenAIClient) -> None:
        self._client = client

    def chat_completion(self, prompt: str) -> str:
        return self._client.chat_completion(prompt)

    def generate_response(self, prompt: str) -> str:
        """Same duck-typed contract as ``MistralApiService.generate_response``."""
        return self._client.chat_completion(prompt)

    def close(self) -> None:
        """Release the underlying HTTP client (called after the request completes)."""
        self._client.close()
