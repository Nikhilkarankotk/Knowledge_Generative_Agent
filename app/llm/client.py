"""HTTP client for the Mistral AI API.

Replicates the behaviour of the two ``WebClient`` instances used by the Java code:
``MistralApiService`` (chat completions) and ``EmbeddingService`` (embeddings), as well
as the multipart file upload performed by ``MistralService``.

Retry behaviour mirrors ``.timeout(Duration.ofSeconds(30)).retry(3)`` from Reactor: a
request is attempted once and retried up to ``retries`` additional times on failure.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import Settings
from app.core.exceptions import MistralApiError

logger = logging.getLogger(__name__)


class MistralClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.mistral.ai/v1",
        timeout_seconds: float = 30.0,
        retries: int = 3,
        chat_model: str = "mistral-small-latest",
        embedding_model: str = "mistral-embed",
        ocr_model: str = "mistral-ocr-latest",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._chat_model = chat_model
        self._embedding_model = embedding_model
        self._ocr_model = ocr_model
        self._retries = retries
        self._client = httpx.Client(
            base_url=self._base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_seconds,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> MistralClient:
        return cls(
            api_key=settings.mistral_api_key,
            base_url=settings.mistral_base_url,
            timeout_seconds=settings.mistral_timeout_seconds,
            retries=settings.mistral_retries,
            chat_model=settings.mistral_chat_model,
            embedding_model=settings.mistral_embedding_model,
            ocr_model=settings.mistral_ocr_model,
        )

    def close(self) -> None:
        self._client.close()

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        files: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    files=files,
                    headers=headers,
                )
                response.raise_for_status()
                return response
            except Exception as exc:  # noqa: BLE001 - WebClient also retries on any error
                last_error = exc
                logger.warning("Mistral API call failed (%s) - attempt %d/%d",
                               url, attempt + 1, self._retries + 1)
        raise MistralApiError(f"Mistral API request to {url} failed: {last_error}") from last_error

    def _post_with_retry(
        self,
        url: str,
        *,
        json: dict | None = None,
        files: dict | None = None,
        headers: dict | None = None,
    ) -> httpx.Response:
        return self._request_with_retry(
            "POST", url, json=json, files=files, headers=headers
        )

    def chat_completion(self, prompt: str) -> str:
        """POST /chat/completions with a single user message (Java-compatible body)."""
        payload = {
            "model": self._chat_model,
            "messages": [{"role": "user", "content": prompt}],
        }
        response = self._post_with_retry("/chat/completions", json=payload)
        data = response.json()
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise MistralApiError(
                "Unexpected /chat/completions response: missing choices[0].message.content"
            ) from exc

    def generate_embedding(self, text: str) -> list[float]:
        """POST /embeddings using the configured embedding model."""
        payload = {"model": self._embedding_model, "input": text}
        response = self._post_with_retry("/embeddings", json=payload)
        data = response.json()
        try:
            embedding = data["data"][0]["embedding"]
            logger.debug("Generated embedding (%d dims)", len(embedding))
            return [float(value) for value in embedding]
        except (KeyError, IndexError, TypeError) as exc:
            raise MistralApiError(
                "Unexpected /embeddings response: missing data[0].embedding"
            ) from exc

    def upload_file(self, filename: str, content: bytes) -> str:
        """POST /files (multipart) with ``purpose=ocr``; returns the file id."""
        response = self._post_with_retry(
            "/files",
            files={"file": (filename, content), "purpose": (None, "ocr")},
        )
        data = response.json()
        file_id = data.get("id", "")
        if not file_id:
            raise MistralApiError("Failed to parse file_id from Mistral response")
        return str(file_id)

    def ocr_process(self, file_id: str, message: str) -> str:
        """OCR an uploaded file, equivalent of ``MistralService.java`` step 2.

        The Java code called the retired ``POST /v1/ocr/process`` endpoint. Mistral's
        current API requires resolving an uploaded file to a signed URL first
        (``GET /v1/files/{id}/url``) and then calling ``POST /v1/ocr`` with that URL as a
        ``document_url``; the response exposes per-page markdown that is joined here. The
        historical ``message``/instruction is not a parameter of the modern OCR request.
        """
        signed = self._request_with_retry(
            "GET", f"/files/{file_id}/url", params={"expiry": "1"}
        )
        signed_data = signed.json()
        signed_url = (signed_data or {}).get("url", "")
        if not signed_url:
            raise MistralApiError("Failed to parse signed URL from Mistral response")
        payload = {
            "model": self._ocr_model,
            "document": {"type": "document_url", "document_url": signed_url},
        }
        response = self._request_with_retry("POST", "/ocr", json=payload)
        data = response.json()
        pages = data.get("pages") if isinstance(data, dict) else None
        if pages is None:
            raise MistralApiError("Unexpected /v1/ocr response: missing pages")
        return "\n\n".join(
            str(page.get("markdown", ""))
            for page in pages
            if isinstance(page, dict)
        ).strip()
