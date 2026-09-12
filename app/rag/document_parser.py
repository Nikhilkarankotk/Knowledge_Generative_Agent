"""Equivalent of ``DocumentParser.java`` — extended to support multiple formats.

Supported:
- PDF (.pdf): ``pypdf``
- DOCX (.docx): ``python-docx``
- XLSX (.xlsx): stdlib ``zipfile`` + XML text extraction
- PPTX (.pptx): stdlib ``zipfile`` + XML text extraction
- Plain text (.txt, .md, .log)
- CSV / TSV (.csv, .tsv)
- JSON (.json)
- HTML / HTM (.html, .htm): tag stripping via stdlib ``html.parser``

Image files (.png, .jpg, .jpeg, etc.) are **not** parsed here; the ``RagService``
routes them through Mistral OCR instead.  The parser exposes an ``is_image`` method
for that purpose.

Unsupported or corrupt formats raise ``UnsupportedFileTypeError`` (→ HTTP 400).
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import xml.sax.saxutils
import zipfile
from html.parser import HTMLParser

from app.core.exceptions import UnsupportedFileTypeError

logger = logging.getLogger(__name__)

_PDF_SIG = b"%PDF-"
_DOCX_ZIP_CONTENT = "word/document.xml"  # entry name inside a .docx zip
_TEXT_RE = re.compile(r"<[^>]*>([^<]*)", re.S)  # generic XML text node extraction


class _HTMLTextStripper(HTMLParser):
    """Minimal HTML tag stripper that collects character data."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


class DocumentParser:
    IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tiff", ".tif"})
    TEXT_EXTENSIONS = frozenset({".txt", ".md", ".log", ".rst"})
    CSV_EXTENSIONS = frozenset({".csv", ".tsv"})
    JSON_EXTENSIONS = frozenset({".json"})
    HTML_EXTENSIONS = frozenset({".html", ".htm"})
    OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx", ".pptx"})
    PDF_EXTENSIONS = frozenset({".pdf"})
    ALL_EXTENSIONS = (
        PDF_EXTENSIONS | OFFICE_EXTENSIONS | IMAGE_EXTENSIONS
        | TEXT_EXTENSIONS | CSV_EXTENSIONS | JSON_EXTENSIONS | HTML_EXTENSIONS
    )

    # ---- detection helpers ---------------------------------------------------

    @staticmethod
    def _extension(filename: str | None) -> str:
        if not filename:
            return ""
        return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    @staticmethod
    def _looks_like_pdf(content: bytes) -> bool:
        return content[:5] == _PDF_SIG

    @staticmethod
    def _looks_like_zip(content: bytes) -> bool:
        return content[:4] == b"PK\x03\x04"

    @staticmethod
    def _looks_like_image(content: bytes) -> bool:
        return (
            content[:8] == b"\x89PNG\r\n\x1a\n"
            or content[:3] == b"\xff\xd8\xff"
            or content[:4] == b"GIF8"
            or content[:2] == b"BM"
            or (len(content) > 12 and content[8:12] == b"WEBP")
            or content[:4] == b"II\x2a\x00"
            or content[:4] == b"MM\x00\x2a"
        )

    @staticmethod
    def _looks_like_html(content: bytes) -> bool:
        snippet = content[:1024].decode("utf-8", errors="ignore").strip().lower()
        return snippet.startswith("<!doctype html") or snippet.startswith("<html")

    def is_image(self, content: bytes, filename: str | None) -> bool:
        ext = "." + self._extension(filename) if self._extension(filename) else ""
        return self._looks_like_image(content) or ext in self.IMAGE_EXTENSIONS

    @property
    def supported_extensions(self) -> str:
        return ", ".join(sorted(ext.lstrip(".") for ext in sorted(self.ALL_EXTENSIONS)))

    # ---- entry point ---------------------------------------------------------

    def parse_document(self, content: bytes, filename: str | None) -> str:
        logger.debug("=== Starting Document Parsing ===: %s", filename)
        ext = "." + self._extension(filename) if self._extension(filename) else ""
        # 1. PDF
        if ext in self.PDF_EXTENSIONS or self._looks_like_pdf(content):
            logger.debug("Parsing PDF file...")
            return self._parse_pdf(content)
        # 2. Images → should be routed by RagService before reaching here
        if self._looks_like_image(content) or ext in self.IMAGE_EXTENSIONS:
            raise UnsupportedFileTypeError(
                "Image files require OCR and cannot be ingested as plain text."
            )
        # 3. Office zip (DOCX/XLSX/PPTX detected by zip content or extension)
        if self._looks_like_zip(content) or ext in self.OFFICE_EXTENSIONS:
            return self._parse_office_zip(content, ext)
        # 4. HTML
        if ext in self.HTML_EXTENSIONS or self._looks_like_html(content):
            logger.debug("Parsing HTML file...")
            return self._parse_html(content)
        # 5. JSON
        if ext in self.JSON_EXTENSIONS:
            logger.debug("Parsing JSON file...")
            return self._parse_json(content)
        # 6. CSV / TSV
        if ext in self.CSV_EXTENSIONS:
            logger.debug("Parsing CSV/TSV file...")
            return self._parse_csv(content, ext)
        # 7. Plain text
        if ext in self.TEXT_EXTENSIONS:
            logger.debug("Parsing text file...")
            return self._parse_text(content)
        # 8. Unsupported
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{filename}'. "
            f"Supported formats: {self.supported_extensions}"
        )

    # ---- format handlers -----------------------------------------------------

    def _parse_pdf(self, content: bytes) -> str:
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                from app.core.exceptions import IllegalArgumentException
                raise IllegalArgumentException(
                    "The PDF is password-protected and cannot be parsed."
                )
            pages = []
            for page in reader.pages:
                try:
                    pages.append(page.extract_text() or "")
                except Exception:
                    pages.append("")
            text = "\n".join(pages)
            if text.strip() == "":
                from app.core.exceptions import IllegalArgumentException
                raise IllegalArgumentException(
                    "Parsed text is empty. The PDF may be image-only."
                )
            return text
        except UnsupportedFileTypeError:
            raise
        except Exception as exc:
            from app.core.exceptions import IllegalArgumentException
            logger.error("PDF Parsing Failed: %s", exc)
            raise IllegalArgumentException(f"PDF parsing failed: {exc}") from exc

    def _parse_docx(self, content: bytes) -> str:
        from docx import Document as DocxDocument

        try:
            document = DocxDocument(io.BytesIO(content))
            paragraphs = [paragraph.text for paragraph in document.paragraphs]
            text = "\n".join(paragraphs)
            if text is None or text.strip() == "":
                raise UnsupportedFileTypeError("DOCX file contains no readable text.")
            return text
        except UnsupportedFileTypeError:
            raise
        except Exception as exc:
            logger.error("DOCX Parsing Failed: %s", exc)
            raise UnsupportedFileTypeError(f"Document is not a valid DOCX file: {exc}") from exc

    # ---- Office ZIP helpers --------------------------------------------------

    def _parse_office_zip(self, content: bytes, ext: str) -> str:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                names = zf.namelist()
                # Prefer extension-guided parsing when available
                if ext == ".docx" or (ext == "" and any(_DOCX_ZIP_CONTENT in n for n in names)):
                    return self._parse_docx(content)
                if ext == ".xlsx" or (ext == "" and any(n.startswith("xl/") for n in names)):
                    return self._extract_xlsx_text(zf)
                if ext == ".pptx" or (ext == "" and any(n.startswith("ppt/") for n in names)):
                    return self._extract_pptx_text(zf)
                # Unknown zip content – try docx as legacy fallback
                if ext == ".docx":
                    return self._parse_docx(content)
                raise UnsupportedFileTypeError(
                    f"ZIP archive '{ext or '(no extension)'}' is not a supported Office format."
                )
        except UnsupportedFileTypeError:
            raise
        except zipfile.BadZipFile as exc:
            raise UnsupportedFileTypeError(f"Document is not a valid Office file: {exc}") from exc

    @staticmethod
    def _extract_xlsx_text(zf: zipfile.ZipFile) -> str:
        """Extract text from shared strings + inline cells in an XLSX archive."""
        parts: list[str] = []
        for name in zf.namelist():
            if name.startswith("xl/") and name.endswith(".xml"):
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                    parts.extend(_TEXT_RE.findall(raw))
                except Exception:
                    continue
        text = "\n".join(t for t in parts if t.strip())
        if not text.strip():
            raise UnsupportedFileTypeError("XLSX file contains no readable text.")
        return text

    @staticmethod
    def _extract_pptx_text(zf: zipfile.ZipFile) -> str:
        """Extract text from slide XML in a PPTX archive."""
        parts: list[str] = []
        for name in zf.namelist():
            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                try:
                    raw = zf.read(name).decode("utf-8", errors="ignore")
                    parts.extend(_TEXT_RE.findall(raw))
                except Exception:
                    continue
        text = "\n".join(t for t in parts if t.strip())
        if not text.strip():
            raise UnsupportedFileTypeError("PPTX file contains no readable text.")
        return text

    # ---- text / HTML / CSV / JSON -------------------------------------------

    @staticmethod
    def _parse_text(content: bytes) -> str:
        text = content.decode("utf-8", errors="replace")
        if not text.strip():
            raise UnsupportedFileTypeError("File contains no readable text.")
        return text

    @staticmethod
    def _parse_html(content: bytes) -> str:
        raw = content.decode("utf-8", errors="replace")
        stripper = _HTMLTextStripper()
        try:
            stripper.feed(raw)
        except Exception:
            pass
        text = stripper.get_text()
        if not text.strip():
            raise UnsupportedFileTypeError("HTML file contains no readable text.")
        return xml.sax.saxutils.unescape(text)

    @staticmethod
    def _parse_csv(content: bytes, ext: str) -> str:
        raw = content.decode("utf-8", errors="replace")
        delimiter = "\t" if ext == ".tsv" else ","
        try:
            reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
            rows = [" ".join(cell for cell in row if cell.strip()) for row in reader]
        except Exception:
            rows = raw.splitlines()
        text = "\n".join(rows)
        if not text.strip():
            raise UnsupportedFileTypeError("CSV file contains no readable text.")
        return text

    @staticmethod
    def _parse_json(content: bytes) -> str:
        raw = content.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                text = parsed
            else:
                text = json.dumps(parsed, indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, ValueError) as exc:
            raise UnsupportedFileTypeError(f"File is not valid JSON: {exc}") from exc
        if not text.strip():
            raise UnsupportedFileTypeError("JSON file contains no readable text.")
        return text
