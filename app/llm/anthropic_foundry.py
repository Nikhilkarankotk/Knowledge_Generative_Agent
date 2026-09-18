"""HTTP client for an Anthropic model on Azure AI Foundry (Claude Opus / ``luna``).

The Foundry project exposes an Anthropic-compatible Messages surface:

    POST {endpoint}/v1/messages     (endpoint ends in ``.../anthropic``)

with the model name (e.g. ``claude-opus-4-8``) in the request body. Authentication
uses the ``x-api-key`` header and the ``anthropic-version`` header is required.

This client mirrors :class:`app.llm.MistralClient`'s retry/timeout behaviour so a
*separate* Anthropic model can power the GitHub repository analysis narrative
without disturbing the Mistral chat/RAG/embedding path. Only message creation
(chat) is implemented here; it is used purely for synthesis, never for embeddings
or OCR.
"""

from __future__ import annotations

import logging

import httpx

from app.core.exceptions import AnthropicFoundryApiError

logger = logging.getLogger(__name__)


class AnthropicFoundryClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str,
        model: str = "claude-opus-4-8",
        anthropic_version: str = "2023-06-01",
        timeout_seconds: float = 60.0,
        retries: int = 2,
        max_tokens: int = 3000,
        temperature: float | None = None,
    ) -> None:
        if not api_key:
            raise AnthropicFoundryApiError(
                "ANTHROPIC_FOUNDRY_API_KEY is required for the Anthropic Foundry client."
            )
        if not endpoint:
            raise AnthropicFoundryApiError(
                "ANTHROPIC_FOUNDRY_ENDPOINT is required for the Anthropic Foundry client."
            )
        if not model:
            raise AnthropicFoundryApiError(
                "ANTHROPIC_FOUNDRY_MODEL is required for the Anthropic Foundry client."
            )
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._retries = retries
        self._client = httpx.Client(
            base_url=self._endpoint,
            headers={
                "x-api-key": api_key,
                "anthropic-version": anthropic_version,
                "Content-Type": "application/json",
            },
            timeout=timeout_seconds,
        )

    @classmethod
    def from_settings(cls, settings) -> "AnthropicFoundryClient":  # noqa: ANN001 - Settings (avoid import cycle)
        temperature = getattr(settings, "anthropic_foundry_temperature", None)
        return cls(
            api_key=settings.anthropic_foundry_api_key,
            endpoint=settings.anthropic_foundry_endpoint,
            model=settings.anthropic_foundry_model,
            anthropic_version=settings.anthropic_foundry_version,
            timeout_seconds=settings.anthropic_foundry_timeout_seconds,
            retries=settings.anthropic_foundry_retries,
            max_tokens=settings.github_analysis_max_tokens,
            temperature=temperature,
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
                logger.warning(
                    "Anthropic Foundry API call failed (%s) - attempt %d/%d",
                    url,
                    attempt + 1,
                    self._retries + 1,
                )
        raise AnthropicFoundryApiError(
            f"Anthropic Foundry API request to {url} failed: {last_error}"
        ) from last_error

    def chat_completion(self, prompt: str) -> str:
        """POST /v1/messages with a single user message (Anthropic Messages body)."""
        payload: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": self._max_tokens,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        response = self._post_with_retry("/v1/messages", json=payload)
        data = response.json()
        try:
            # Anthropic returns a list of content blocks; concatenate the text ones.
            blocks = data["content"]
            text = "".join(
                str(block.get("text", ""))
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if not text:
                raise KeyError("no text content blocks")
            return text
        except (KeyError, IndexError, TypeError) as exc:
            raise AnthropicFoundryApiError(
                "Unexpected /v1/messages response: missing content[].text"
            ) from exc
