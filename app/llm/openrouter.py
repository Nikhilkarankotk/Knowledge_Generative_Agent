"""HTTP client for the OpenRouter API.

OpenRouter exposes an OpenAI-compatible ``/chat/completions`` endpoint. This client
mirrors :class:`app.llm.MistralClient`'s retry/timeout behaviour (60s timeout, a
configurable number of retries) so a *separate* LLM can power the GitHub repository
analysis narrative without disturbing the Mistral chat/RAG/embedding path.

Only chat completions are implemented here; OpenRouter models are used purely for
synthesis, never for embeddings or OCR.
"""

from __future__ import annotations

import logging

import httpx

from app.core.exceptions import OpenRouterApiError

logger = logging.getLogger(__name__)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 60.0,
        retries: int = 2,
        model: str = "openrouter/free",
        max_tokens: int = 3000,
        temperature: float = 0.1,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._retries = retries
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    def close(self) -> None:
        self._client.close()

    def _post_with_retry(self, url: str, *, json: dict | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._client.post(url, json=json)
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001 - retry on any transport/HTTP error
                last_error = exc
                logger.warning("OpenRouter API call failed (%s) - attempt %d/%d",
                               url, attempt + 1, self._retries + 1)
        raise OpenRouterApiError(f"OpenRouter API request to {url} failed: {last_error}") from last_error

    def chat_completion(self, prompt: str) -> str:
        """POST /chat/completions with a single user message (OpenAI-compatible body)."""
        payload = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
        }
        response = self._post_with_retry("/chat/completions", json=payload)
        data = response.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise OpenRouterApiError(
                "Unexpected /chat/completions response: missing choices[0].message.content"
            ) from exc
