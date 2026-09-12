"""Equivalent of ``MistralService.java`` (file upload + OCR processing)."""

from __future__ import annotations

from app.llm import MistralClient


class MistralService:
    """Uploads a document to Mistral and sends a message to its OCR processing endpoint."""

    def __init__(self, client: MistralClient) -> None:
        self._client = client

    def upload_document_and_send_message(self, filename: str, content: bytes, message: str) -> str:
        """Replicates ``uploadDocumentAndSendMessage(MultipartFile, String)``."""
        file_id = self._client.upload_file(filename, content)
        return self._client.ocr_process(file_id, message)
