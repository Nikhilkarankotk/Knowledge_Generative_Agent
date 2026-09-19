"""Typed errors for the source-aware export."""

from __future__ import annotations


class ExportError(Exception):
    """Base error; safe to surface to API clients (never contains secrets)."""


class ExportNotFoundError(ExportError):
    """No retrievable context/sources for the requested chat message."""


class ExportNotAllowedError(ExportError):
    """The requested export is outside the allowed scope (e.g. wrong session)."""


class ExportLimitError(ExportError):
    """The export exceeded a size/file/traversal limit and was refused."""


class ExportValidationError(ExportError):
    """An export intent or artifact failed validation."""
