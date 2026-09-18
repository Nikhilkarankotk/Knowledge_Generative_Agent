"""Adapter that exposes the Anthropic Foundry client through the LLM service API.

Keeps the same duck-typed ``generate_response(prompt)`` contract the rest of the
application expects (``MistralApiService``/``OpenRouterApiService`` use the
identical signature), so ``ReportSynthesizer`` can be handed this service without
changing its code.
"""

from __future__ import annotations

from app.llm.anthropic_foundry import AnthropicFoundryClient


class AnthropicFoundryApiService:
    """Generates chat responses via an Anthropic model on Azure AI Foundry."""

    def __init__(self, client: AnthropicFoundryClient) -> None:
        self._client = client

    def chat_completion(self, prompt: str) -> str:
        return self._client.chat_completion(prompt)

    def generate_response(self, prompt: str) -> str:
        """Same duck-typed contract as ``MistralApiService.generate_response``."""
        return self._client.chat_completion(prompt)

    def close(self) -> None:
        """Release the underlying HTTP client (called after the request completes)."""
        self._client.close()
