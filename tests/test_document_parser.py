"""Tests for :mod:`app.rag.document_parser` (DocumentParser.java + extended formats)."""

import pytest

from app.core.exceptions import IllegalArgumentException, UnsupportedFileTypeError
from app.rag.document_parser import DocumentParser
from tests.conftest import (
    make_docx_with_text,
    make_pdf_with_text,
    make_png_bytes,
    make_pptx_with_text,
    make_xlsx_with_text,
)


@pytest.fixture
def parser() -> DocumentParser:
    return DocumentParser()


def test_parses_pdf_based_on_extension(parser: DocumentParser) -> None:
    text = parser.parse_document(make_pdf_with_text("Portfolio PDF content"), "resume.PDF")
    assert "Portfolio PDF content" in text


def test_parses_docx_for_non_pdf(parser: DocumentParser) -> None:
    text = parser.parse_document(make_docx_with_text("Hello from DOCX"), "notes.docx")
    assert "Hello from DOCX" in text


def test_non_pdf_without_extension_is_treated_as_docx(parser: DocumentParser) -> None:
    text = parser.parse_document(make_docx_with_text("Docx by default"), "file")
    assert "Docx by default" in text


def test_empty_docx_raises_docx_message(parser: DocumentParser) -> None:
    with pytest.raises(IllegalArgumentException, match="DOCX file contains no readable text"):
        parser.parse_document(make_docx_with_text(""), "blank.docx")


def test_invalid_docx_raises(parser: DocumentParser) -> None:
    with pytest.raises(IllegalArgumentException):
        parser.parse_document(b"this is not a zip file at all", "broken.docx")


def test_not_a_pdf_raises(parser: DocumentParser) -> None:
    with pytest.raises(IllegalArgumentException):
        parser.parse_document(b"<html>not a pdf</html>", "fake.pdf")


def test_parses_txt(parser: DocumentParser) -> None:
    text = parser.parse_document(b"Hello plain text", "notes.txt")
    assert "Hello plain text" in text


def test_parses_markdown(parser: DocumentParser) -> None:
    text = parser.parse_document(b"# Title\nSome **bold** text", "readme.md")
    assert "Title" in text
    assert "Some" in text


def test_parses_html_strips_tags(parser: DocumentParser) -> None:
    text = parser.parse_document(b"<html><body><p>Hello <b>World</b></p></body></html>", "page.html")
    assert "Hello" in text
    assert "World" in text
    assert "<b>" not in text


def test_parses_csv(parser: DocumentParser) -> None:
    text = parser.parse_document(b"name,role\nalice,engineer\nbob,pm", "team.csv")
    assert "name" in text
    assert "alice" in text
    assert "bob" in text


def test_parses_json(parser: DocumentParser) -> None:
    text = parser.parse_document(b'{"name":"Karan","years":5}', "profile.json")
    assert "name" in text
    assert "Karan" in text


def test_parses_xlsx(parser: DocumentParser) -> None:
    text = parser.parse_document(make_xlsx_with_text("Cell value here"), "data.xlsx")
    assert "Cell value here" in text


def test_parses_pptx(parser: DocumentParser) -> None:
    text = parser.parse_document(make_pptx_with_text("Slide one text"), "deck.pptx")
    assert "Slide one text" in text


def test_detects_image_by_signature_and_extension(parser: DocumentParser) -> None:
    assert parser.is_image(make_png_bytes(), "scan.png") is True
    assert parser.is_image(make_png_bytes(), "photo") is True
    assert parser.is_image(make_pdf_with_text("x"), "scan.png") is True  # by extension
    assert parser.is_image(make_pdf_with_text("x"), "doc.pdf") is False


def test_parse_document_rejects_image_with_ocr_hint(parser: DocumentParser) -> None:
    with pytest.raises(UnsupportedFileTypeError, match="OCR"):
        parser.parse_document(make_png_bytes(), "scan.png")


def test_unsupported_type_raises_helpful_message(parser: DocumentParser) -> None:
    with pytest.raises(UnsupportedFileTypeError, match="Unsupported file type"):
        parser.parse_document(b"\x00\x01\x02binary", "file.xyz")
