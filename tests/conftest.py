"""Shared pytest fixtures.

Environment variables are set **before** any ``app`` module is imported so that the
application boots against an in-memory SQLite database and a fake Mistral API key.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Generator
from pathlib import Path

# --- Must happen before any app import ------------------------------------------------
os.environ["DATABASE_URL"] = "sqlite+pysqlite://"
os.environ["MISTRAL_API_KEY"] = "test-api-key"
os.environ["LOG_LEVEL"] = "ERROR"
os.environ["MISTRAL_TIMEOUT_SECONDS"] = "5"
# Keep the legacy augmented-prompt path for the existing suite; Semantic Kernel agent
# paths are tested explicitly with scripted chat services (no network).
os.environ["SK_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies
from app.main import app as fastapi_app
from app.models import Base

# --- Minimal PDF/DOCX builders --------------------------------------------------------


def make_pdf_with_text(text: str) -> bytes:
    """Build a minimal valid single-page PDF containing ``text`` (pypdf-compatible)."""
    objects = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    content = f"BT /F1 24 Tf 100 700 Td ({text}) Tj ET".encode("latin-1")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>"
    )
    objects.append(b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    body = bytearray()
    offsets: list[int] = []
    body.extend(b"%PDF-1.4\n")
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body.extend(f"{index} 0 obj\n".encode())
        body.extend(obj)
        body.extend(b"\nendobj\n")

    xref_offset = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets:
        body.extend(f"{offset:010d} 00000 n \n".encode())
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(body)


def make_docx_with_text(text: str) -> bytes:
    """Build a small .docx (via python-docx) containing ``text``."""
    from docx import Document

    stream = io.BytesIO()
    document = Document()
    document.add_paragraph(text)
    document.save(stream)
    return stream.getvalue()


def make_xlsx_with_text(text: str) -> bytes:
    """Build a minimal .xlsx (real ZIP) whose worksheets contain ``text``."""
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr(
            "xl/sharedStrings.xml",
            f'<?xml version="1.0"?><sst><si><t>{text}</t></si></sst>',
        )
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet><sheetData><row><c><v>1</v></c></row></sheetData></worksheet>',
        )
    return stream.getvalue()


def make_pptx_with_text(text: str) -> bytes:
    """Build a minimal .pptx (real ZIP) whose slide contains ``text``."""
    import zipfile

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<?xml version='1.0'?><Types/>")
        zf.writestr(
            "ppt/slides/slide1.xml",
            f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            f'<p:cSld><p:spTree><p:sp><p:txBody><a:p xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>",
        )
    return stream.getvalue()


def make_png_bytes() -> bytes:
    """Bytes that carry a PNG signature (enough for format detection)."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16


class FakeLLM:
    """Deterministic stand-in for the Mistral API used across the test-suite."""

    def __init__(self) -> None:
        self.chat_requests: list[str] = []
        self.chat_responses: list[str] = []
        self.chat_response = "Test assistant response"
        self.embedding_map: dict[str, list[float]] = {}
        self.upload_file_id = "file-test-123"
        self.ocr_response = "OCR text response"

    def chat_completion(self, prompt: str) -> str:
        self.chat_requests.append(prompt)
        if self.chat_responses:
            return self.chat_responses.pop(0)
        return self.chat_response

    def generate_embedding(self, text: str) -> list[float]:
        if text in self.embedding_map:
            return self.embedding_map[text]
        # Deterministic cheap vector derived from the text content.
        vector = [0.0] * 16
        for index, char in enumerate(text):
            vector[index % len(vector)] += float(ord(char))
        return vector

    def upload_file(self, filename: str, content: bytes) -> str:
        return self.upload_file_id

    def ocr_process(self, file_id: str, message: str) -> str:
        return self.ocr_response


@pytest.fixture(autouse=True)
def clean_db() -> Generator[None, None, None]:
    """Recreate the schema before every test."""
    Base.metadata.drop_all(dependencies._database.engine)
    Base.metadata.create_all(dependencies._database.engine)
    yield


@pytest.fixture
def db_session():
    session = dependencies._database.create_session()
    try:
        yield session
        session.commit()
    finally:
        session.close()


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def api_llm(fake_llm: FakeLLM) -> FakeLLM:
    """Wire the real services up to the fake LLM (like replacing the httpx client)."""
    from app.rag.embedding_service import EmbeddingService
    from app.services.mistral_api_service import MistralApiService
    from app.services.mistral_service import MistralService

    originals = {
        "mistral_api_service": dependencies._mistral_api_service,
        "embedding_service": dependencies._embedding_service,
        "mistral_service": dependencies._mistral_service,
    }

    dependencies._mistral_api_service = MistralApiService(fake_llm)  # type: ignore[attr-defined]
    dependencies._embedding_service = EmbeddingService(fake_llm)  # type: ignore[attr-defined]
    dependencies._mistral_service = MistralService(fake_llm)  # type: ignore[attr-defined]
    yield fake_llm
    dependencies._mistral_api_service = originals["mistral_api_service"]
    dependencies._embedding_service = originals["embedding_service"]
    dependencies._mistral_service = originals["mistral_service"]
