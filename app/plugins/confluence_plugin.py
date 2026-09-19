"""ConfluencePlugin - exposes Confluence knowledge search to the agent.

Delegates to :class:`~app.services.confluence_service.ConfluenceService`, which owns
authentication, request building and result formatting. The plugin never raises an
unexpected error into the auto-invocation loop: Confluence failures are translated
into honest, non-fabricated markers so the agent can answer from the sources that
are still available.
"""

from __future__ import annotations

import logging
from typing import Any

from semantic_kernel.functions import kernel_function

from app.core.exceptions import ConfluenceApiError
from app.export.capture import ExportItemDraft, RetrievalCapture, parse_confluence_page, parse_confluence_search
from app.export.formats import ScenarioType, SourceType
from app.services.confluence_service import ConfluenceService

# The @kernel_function decorator below runs signature introspection at class-definition
# time, which crashes on Python 3.14 unless the compatibility patch is applied first.
from app.sk.compat import apply_py314_compatibility_patch

apply_py314_compatibility_patch()

logger = logging.getLogger(__name__)

SEARCH_PAGES_DESCRIPTION = (
    "Search Confluence, the authoritative source for organizational and project "
    "documentation: applications, services, architecture and system design, APIs, "
    "deployment and release guides, security guidelines, engineering and technical "
    "documentation, architecture decision records, integrations, incident management "
    "and runbooks, and project documentation. Confluence may contain information "
    "that is not present in the user's uploaded documents. If the question concerns "
    "any of these technical or enterprise topics, invoke this function automatically "
    "without asking the user for permission. Pass space_key (a space key from "
    "list_spaces) to scope the search to one Confluence space, which reduces noise "
    "when many pages exist. If the first search returns no useful pages, call this "
    "function again with broader terms before concluding anything."
)

GET_PAGE_DESCRIPTION = (
    "Retrieve the full content of a Confluence page by its page id. Call this after "
    "search_pages returns a page id and you need more detail from that page."
)

LIST_SPACES_DESCRIPTION = (
    "List the Confluence spaces the agent can search (space keys and names). Call "
    "this before searching when you are unsure which space holds the content, or "
    "when a wiki has many pages: scoping search_pages with space_key afterwards "
    "gives more relevant results. Invoke this automatically; do not ask the user."
)


class ConfluencePlugin:
    """Search and read Confluence pages through the ConfluenceService."""

    def __init__(
        self,
        confluence_service: ConfluenceService | None,
        capture: RetrievalCapture | None = None,
    ) -> None:
        self._service = confluence_service
        self._capture = capture

    @kernel_function(description=SEARCH_PAGES_DESCRIPTION, name="search_pages")
    def search_pages(
        self,
        query: str,
        limit: int | None = None,
        space_key: str | None = None,
    ) -> str:
        if not self._enabled:
            logger.info("Confluence search_pages not invoked: Confluence not configured")
            return "Confluence search is not configured for this deployment."
        logger.info(
            "Confluence search_pages invoked: query=%r space_key=%r limit=%r",
            query,
            space_key,
            limit,
        )
        try:
            result = self._service.search(  # type: ignore[union-attr]
                query, limit=limit, space_key=space_key
            )
            _capture_search_results(self._capture, result)
            logger.info(
                "Confluence search_pages completed: %d results returned",
                _count_sources(result),
            )
            logger.debug(
                "Confluence search_pages tool result: %r", self._redact(result)
            )
            return result
        except ConfluenceApiError as exc:
            logger.warning("Confluence search failed: %s", exc)
            return f"Confluence search is currently unavailable: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected Confluence search failure")
            return "Confluence search is currently unavailable."

    @kernel_function(description=LIST_SPACES_DESCRIPTION, name="list_spaces")
    def list_spaces(self, limit: int = 50) -> str:
        if not self._enabled:
            logger.info("Confluence list_spaces not invoked: Confluence not configured")
            return "Confluence access is not configured for this deployment."
        logger.info("Confluence list_spaces invoked: limit=%r", limit)
        try:
            result = self._service.list_spaces(limit=limit)  # type: ignore[union-attr]
            logger.info(
                "Confluence list_spaces completed: %d results returned",
                _count_sources(result),
            )
            return result
        except ConfluenceApiError as exc:
            logger.warning("Confluence list_spaces failed: %s", exc)
            return f"Confluence spaces are currently unavailable: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected Confluence list_spaces failure")
            return "Confluence spaces are currently unavailable."

    @kernel_function(description=GET_PAGE_DESCRIPTION, name="get_page")
    def get_page(self, page_id: str) -> str:
        if not self._enabled:
            logger.info("Confluence get_page not invoked: Confluence not configured")
            return "Confluence access is not configured for this deployment."
        logger.info("Confluence get_page invoked: page_id=%r", page_id)
        try:
            result = self._service.get_page(page_id)  # type: ignore[union-attr]
            _capture_page_result(self._capture, page_id, result)
            logger.info("Confluence get_page completed: page_id=%r", page_id)
            return result
        except ConfluenceApiError as exc:
            logger.warning("Confluence get_page(%s) failed: %s", page_id, exc)
            return f"Could not retrieve Confluence page {page_id}: {exc}"
        except Exception:  # noqa: BLE001
            logger.exception("Unexpected Confluence get_page failure")
            return f"Could not retrieve Confluence page {page_id}."

    @property
    def _enabled(self) -> bool:
        return self._service is not None and self._service.enabled

    @staticmethod
    def _redact(text: str) -> str:
        """Strip anything that looks like a bearer/basic token before logging."""
        import re

        return re.sub(r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+)", "<redacted>", text or "")


def _capture_search_results(capture: RetrievalCapture | None, result: str) -> None:
    """Persist each returned Confluence page into the turn's export capture."""
    if capture is None:
        return
    for page in parse_confluence_search(result):
        page_id = page.get("page_id")
        if not page_id:
            continue
        metadata: dict[str, Any] = {}
        if page.get("space"):
            metadata["space"] = page["space"]
        if page.get("parent"):
            metadata["parent"] = page["parent"]
        draft = ExportItemDraft(
            source_type=SourceType.CONFLUENCE.value,
            source_id=str(page_id),
            source_name=str(page.get("title") or page_id),
            source_url=page.get("url"),
            metadata=metadata,
            export_strategy=ScenarioType.GENERATED_DOCUMENT.value,
        )
        if page.get("excerpt"):
            draft.merge_content(str(page["excerpt"]))
        capture.add(draft)


def _capture_page_result(capture: RetrievalCapture | None, page_id: str, result: str) -> None:
    """Persist the retrieved Confluence page into the turn's export capture."""
    if capture is None:
        return
    parsed = parse_confluence_page(result)
    if not parsed:
        return
    draft = ExportItemDraft(
        source_type=SourceType.CONFLUENCE.value,
        source_id=page_id,
        source_name=str(parsed.get("title") or page_id),
        source_url=parsed.get("url"),
        export_strategy=ScenarioType.GENERATED_DOCUMENT.value,
    )
    if parsed.get("content"):
        draft.merge_content(str(parsed["content"]))
    capture.add(draft)


def _count_sources(text: str) -> int:
    return text.count("[Source")
