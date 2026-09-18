"""Canonical export taxonomy and safety allowlists.

The source-aware export is driven entirely by *actual retrieved artifacts*
(:class:`app.repositories.export_context_repository.ExportContextItem` rows) and
never by arbitrary strings from a prompt. These constants are the single source of
truth for the source types, scenario types, supported export formats, their
extensions/MIME types, and the filename sanitizer used for every generated ZIP
entry and Content-Disposition header.
"""

from __future__ import annotations

import re
from enum import StrEnum


class SourceType(StrEnum):
    """The four canonical knowledge source types (upper-case strings)."""

    UPLOADED_DOCUMENT = "UPLOADED_DOCUMENT"
    CONFLUENCE = "CONFLUENCE"
    GITHUB = "GITHUB"
    SHAREPOINT = "SHAREPOINT"


class ScenarioType(StrEnum):
    """High-level export scenarios the ExportService resolves to."""

    # A single native source artifact, exported in its original format.
    NATIVE_FILE = "NATIVE_FILE"
    # A single source whose artifact is generated into a chosen document format
    # (e.g. a Confluence page -> DOCX/PDF/MD...).
    GENERATED_DOCUMENT = "GENERATED_DOCUMENT"
    # A single source whose content is analyzed into a report (GitHub repo ->
    # functionality/architecture analysis).
    GENERATED_REPORT = "GENERATED_REPORT"
    # One or more artifacts packaged together (ZIP with manifest.json), each
    # preserving its original/generated format.
    MULTI_ARTIFACT = "MULTI_ARTIFACT"


class ExportFormat(StrEnum):
    """Supported export formats.

    ``NATIVE`` means "keep the source's own original format"; it is resolved to an
    explicit format before a single-file export is produced. The format set is
    intentionally closed: an LLM can never introduce a format that has no exporter.
    """

    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    PPTX = "pptx"
    CSV = "csv"
    TXT = "txt"
    MD = "md"
    JSON = "json"
    HTML = "html"
    NATIVE = "native"


# --- Format metadata -------------------------------------------------------

FORMAT_EXTENSION: dict[ExportFormat, str] = {
    ExportFormat.PDF: "pdf",
    ExportFormat.DOCX: "docx",
    ExportFormat.XLSX: "xlsx",
    ExportFormat.PPTX: "pptx",
    ExportFormat.CSV: "csv",
    ExportFormat.TXT: "txt",
    ExportFormat.MD: "md",
    ExportFormat.JSON: "json",
    ExportFormat.HTML: "html",
    ExportFormat.NATIVE: "",  # resolved dynamically
}

MIME_BY_EXTENSION: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
    "html": "text/html",
    "htm": "text/html",
    "xml": "application/xml",
    "zip": "application/zip",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}

# Extension allowlist. Native artifacts whose extension is not in this list are
# still exportable when their bytes are available (any extension preserved for a
# NATIVE_FILE is acceptable *only* if a safe filename can be derived); generated
# exports may only ever use formats below.
GENERATED_EXTENSIONS: frozenset[str] = frozenset(
    {"pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "json", "html"}
)

# --- Filename sanitization -------------------------------------------------

_FILENAME_BAD = re.compile(r"[^A-Za-z0-9._\- \u00C0-\uFFFF]")
_TRAVERSALS = re.compile(r"(^|[\\/])\.\.([\\/]|$)|^[.]+$|[:;<>\"|?*]|[\x00-\x1f\x7f]")


def safe_filename(name: str, default: str = "export", max_length: int = 120) -> str:
    """Return a path-safe base filename (no directories, no traversal).

    Used for every ZIP entry and downloaded filename. Directory separators,
    traversal sequences, drive letters, control characters and reserved shell
    characters are removed or replaced. The result is a single file name that can
    never escape the archive/response root.
    """
    name = (name or "").strip()
    name = name.replace("\\", "/")
    # Only the last path segment may be used (never absolute/drive paths).
    name = name.rsplit("/", 1)[-1] if "/" in name else name
    if name.strip(". ") == "." or name.strip() == "":
        name = default
    name = name.replace("..", ".").strip()
    name = _FILENAME_BAD.sub("_", name)
    if _TRAVERSALS.search(name):
        name = default
    name = name.strip(" .")
    if not name:
        name = default
    if len(name) > max_length:
        stem, dot, ext = name.rpartition(".")
        if dot and len(ext) <= 10:
            name = stem[: max_length - len(ext) - 1] + "." + ext
        else:
            name = name[:max_length]
    return name


def ensure_extension(filename: str, extension: str) -> str:
    """Append ``extension`` (without dot) unless the file already has it."""
    ext = (extension or "").strip().lstrip(".")
    safe = safe_filename(filename)
    if not ext:
        return safe
    if safe.lower().endswith("." + ext.lower()):
        return safe
    return f"{safe}.{ext}"
