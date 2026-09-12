"""Custom exception hierarchy.

The Java application raises ``IllegalArgumentException``, ``IllegalStateException``
and generic ``RuntimeException`` from its services. Those bubbles propagate to the
Spring error handling which by default maps them to HTTP 500 responses. This module
provides Python equivalents so that the exception semantics are preserved and can be
mapped consistently to HTTP responses by the FastAPI application.
"""

from __future__ import annotations


class ChatbotError(Exception):
    """Base class for all application exceptions."""


class IllegalArgumentException(ChatbotError, ValueError):
    """Equivalent of ``java.lang.IllegalArgumentException``."""


class UnsupportedFileTypeError(IllegalArgumentException):
    """Raised when an uploaded file's format is not supported by the document parser."""


class IllegalStateException(ChatbotError, RuntimeError):
    """Equivalent of ``java.lang.IllegalStateException``."""


class ChatMessageNotFoundException(ChatbotError):
    """Raised when a chat message cannot be found (Feedback submit)."""


class MistralApiError(ChatbotError):
    """Raised when the Mistral API request/response handling fails."""
