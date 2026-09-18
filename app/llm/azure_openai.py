"""HTTP client for an Azure AI Foundry chat completions deployment.

The Foundry project exposes an OpenAI-compatible ``/openai/v1`` surface:

    POST {endpoint}/chat/completions   (endpoint ends in ``.../openai/v1``)

with the model/deployment name (e.g. ``gpt-5.6-luna``) sent in the request body.
Authentication uses the ``api-key`` header. This client mirrors
:class:`app.llm.MistralClient`'s retry/timeout behaviour so the RAG *answer
generation* can run on the Foundry ``luna`` deployment while embeddings and OCR
keep using the Mistral client. Only chat completions are implemented here.

Notes on the ``gpt-5.*`` (luna) model family:
* the token cap parameter is ``max_completion_tokens`` (NOT ``max_tokens``);
* only the default ``temperature`` (1) is accepted, so a custom temperature must
  be omitted. ``supports_temperature`` gates whether it is sent at all.
"""

from __future__ import annotations

import logging

import httpx

from app.core.exceptions import AzureOpenAIApiError

logger = logging.getLogger(__name__)


class AzureOpenAIClient:
    def __init__(
        self,
        api_key: str,
        endpoint: str,
        deployment: str,
        api_version: str = "",
        timeout_seconds: float = 60.0,
        retries: int = 2,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        supports_temperature: bool = False,
    ) -> None:
        if not api_key:
            raise AzureOpenAIApiError("AZURE_OPENAI_API_KEY is required for the Azure OpenAI client.")
        if not endpoint:
            raise AzureOpenAIApiError("AZURE_OPENAI_ENDPOINT is required for the Azure OpenAI client.")
        if not deployment:
            raise AzureOpenAIApiError("AZURE_OPENAI_DEPLOYMENT is required for the Azure OpenAI client.")
        self._endpoint = endpoint.rstrip("/")
        self._deployment = deployment
        self._api_version = (api_version or "").strip()
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._supports_temperature = supports_temperature
        self._retries = retries
        self._client = httpx.Client(
            base_url=self._endpoint,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            timeout=timeout_seconds,
        )

    @classmethod
    def from_settings(cls, settings) -> "AzureOpenAIClient":  # noqa: ANN001 - Settings (avoid import cycle)
        return cls(
            api_key=settings.azure_openai_api_key,
            endpoint=settings.azure_openai_endpoint,
            deployment=settings.azure_openai_deployment,
            api_version=settings.azure_openai_api_version,
            timeout_seconds=settings.azure_openai_timeout_seconds,
            retries=settings.azure_openai_retries,
            max_tokens=settings.azure_openai_max_tokens,
            temperature=settings.azure_openai_temperature,
            supports_temperature=settings.azure_openai_supports_temperature,
        )

    def close(self) -> None:
        self._client.close()

    def _chat_url(self) -> str:
        """Build the chat-completions URL for the Foundry ``/openai/v1`` surface.

        The endpoint is expected to already end in ``/openai/v1``; only
        ``/chat/completions`` is appended. When an ``api_version`` is configured
        (classic Azure OpenAI endpoints), it is added as a query parameter.
        """
        url = "/chat/completions"
        if self._api_version:
            url += f"?api-version={self._api_version}"
        return url

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
                    "Azure OpenAI API call failed (%s) - attempt %d/%d",
                    url,
                    attempt + 1,
                    self._retries + 1,
                )
        raise AzureOpenAIApiError(
            f"Azure OpenAI API request to {url} failed: {last_error}"
        ) from last_error

    def chat_completion(self, prompt: str) -> str:
        """POST /chat/completions with a single user message (model in the body)."""
        payload: dict = {
            "model": self._deployment,
            "messages": [{"role": "user", "content": prompt}],
            # gpt-5.* (luna) requires max_completion_tokens, not max_tokens.
            "max_completion_tokens": self._max_tokens,
        }
        # Only send a custom temperature when the model supports it; luna rejects
        # any non-default temperature, so it is omitted by default.
        if self._supports_temperature:
            payload["temperature"] = self._temperature
        response = self._post_with_retry(self._chat_url(), json=payload)
        data = response.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AzureOpenAIApiError(
                "Unexpected /chat/completions response: missing choices[0].message.content"
            ) from exc
