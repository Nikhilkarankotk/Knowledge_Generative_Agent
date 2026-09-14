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


class ConfluenceApiError(ChatbotError):
    """Raised when the Confluence API request/response handling fails."""


class GitHubApiError(ChatbotError):
    """Raised when the GitHub API request/response handling fails."""


class GitHubRepositoryNotAllowedError(GitHubApiError):
    """Raised when a repository is not in the configured GitHub allowlist.

    Distinct from ``GitHubApiError`` so the agent can distinguish "repository not
    configured for this deployment" from transport-level failures and never fall
    back to searching repositories outside the allowlist.
    """


class KnowledgeAgentError(ChatbotError):
    """Raised when the Knowledge Generative Agent cannot produce an answer."""


class SharePointApiError(ChatbotError):
    """Raised when the Microsoft Graph / SharePoint request/response handling fails."""


class SharePointSiteNotAllowedError(SharePointApiError):
    """Raised when a SharePoint site is not in the configured allowlist.

    Distinct from ``SharePointApiError`` so the agent can distinguish "site not
    configured for this deployment" from transport-level failures and never query
    Graph for sites outside the allowlist.
    """


class SharePointFolderNotAllowedError(SharePointApiError):
    """Raised when a SharePoint folder is not in the configured allowlist.

    Raised before any Graph request when a search/list/read operation targets a
    folder outside ``SHAREPOINT_ALLOWED_FOLDERS``, or when a requested document
    does not live under any configured knowledge-base folder. Distinct from
    ``SharePointApiError`` so the agent can distinguish "folder not configured for
    this deployment" from transport-level failures and never traverse outside the
    configured knowledge base.
    """
