"""Equivalent of ``MistralApiService.java`` (chat completions)."""

from __future__ import annotations

from app.llm import MistralClient


class MistralApiService:
    """Generates chat responses via the Mistral API."""

    def __init__(self, client: MistralClient) -> None:
        self._client = client

    def chat_completion(self, prompt: str) -> str:
        return self._client.chat_completion(prompt)

    def generate_response(self, prompt: str) -> str:
        """Equivalent of ``generateResponse`` which blocks on the reactive call."""
        return self._client.chat_completion(prompt)

    def ocr_document(self, filename: str | None, content: bytes) -> str:
        """Upload a file and OCR it. Used by RAG ingest for image files."""
        file_id = self._client.upload_file(filename or "upload", content)
        return self._client.ocr_process(file_id, "Extract text from the uploaded image.")
