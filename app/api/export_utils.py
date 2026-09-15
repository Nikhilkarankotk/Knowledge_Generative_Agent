"""Shared helpers for the export API routes (safe headers + error mapping)."""

from __future__ import annotations

from urllib.parse import quote

from starlette.responses import JSONResponse

from app.export.errors import (
    ExportError,
    ExportLimitError,
    ExportNotAllowedError,
    ExportNotFoundError,
    ExportValidationError,
)

ERROR_STATUS = {
    ExportNotFoundError: 404,
    ExportNotAllowedError: 403,
    ExportLimitError: 413,
    ExportValidationError: 422,
}


def content_disposition(filename: str) -> str:
    """RFC 5987 Content-Disposition header with a readable fallback filename."""
    return f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"


def export_error_response(exc: ExportError) -> JSONResponse:
    """Map an :class:`ExportError` to the typed JSON response used by both routes."""
    status_code = ERROR_STATUS.get(type(exc), 400)
    return JSONResponse(status_code=status_code, content={"message": str(exc)})
