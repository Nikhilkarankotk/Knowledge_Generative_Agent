"""ConfluencePlugin - exposes Confluence knowledge search to the agent.

Delegates to :class:`~app.services.confluence_service.ConfluenceService`, which owns
authentication, request building and result formatting. The plugin never raises an
unexpected error into the auto-invocation loop: Confluence failures are translated
into honest, non-fabricated markers so the agent can answer from the sources that
are still available.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from semantic_kernel.functions import kernel_function

from app.core.exceptions import ConfluenceApiError
from app.export.capture import (
    ExportItemDraft,
    RetrievalCapture,
    parse_confluence_page,
    parse_confluence_search,
)
from app.export.formats import ScenarioType, SourceType
from app.retrieval.state import (
    RetrievalLedger,
    append_state,
    match_known_scope,
)
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


_SPACE_KEY_RE = re.compile(r"key=([^,\s]+)")


def _search_item(page: dict[str, Any]) -> dict[str, Any]:
    """Normalize a parsed search hit into the shared evidence-item shape."""
    metadata = {
        key: page[key]
        for key in ("space", "owner", "last_editor", "modified")
        if page.get(key)
    }
    return {
        "id": page.get("page_id"),
        "title": page.get("title"),
        "url": page.get("url"),
        "parent": page.get("parent"),
        "excerpt": page.get("excerpt"),
        "metadata": metadata,
    }


def _page_item(page_id: str, parsed: dict[str, Any]) -> dict[str, Any]:
    """Normalize a parsed full page into the shared evidence-item shape."""
    metadata = {
        key: parsed[key]
        for key in ("owner", "last_editor", "modified")
        if parsed.get(key)
    }
    return {
        "id": page_id,
        "title": parsed.get("title"),
        "url": parsed.get("url"),
        "metadata": metadata,
    }


def _query_terms(query: str) -> list[str]:
    """Meaningful (length > 2) lower-cased tokens from a query, in order."""
    return [term for term in re.findall(r"[a-z0-9]+", (query or "").casefold()) if len(term) > 2]


def _relevance_label(query: str, page: dict[str, Any]) -> str:
    """Best-effort relevance of a search hit to the query (title/excerpt/keyword)."""
    title = str(page.get("title") or "").casefold()
    excerpt = str(page.get("excerpt") or "").casefold()
    phrase = " ".join((query or "").casefold().split())
    if phrase and phrase in title:
        return "title"
    terms = _query_terms(query)
    if terms and all(term in title for term in terms):
        return "title"
    haystack = f"{title} {excerpt}"
    if terms and sum(1 for term in terms if term in haystack) >= max(1, (len(terms) + 1) // 2):
        return "excerpt"
    return "keyword"


def _parse_space_keys(text: str) -> dict[str, str]:
    """Map casefolded space key -> exact key as returned by list_spaces."""
    keys: dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _SPACE_KEY_RE.search(line)
        if not match:
            continue
        key = match.group(1).strip().strip('"')
        if key:
            keys[key.casefold()] = key
    return keys


class ConfluencePlugin:
    """Search and read Confluence pages through the ConfluenceService.

    The plugin keeps a per-turn evidence ledger: every page surfaced by a search
    (and every page whose content was retrieved) is remembered and summarized in a
    machine-readable state block appended to the tool result. This guarantees a
    later search that returns zero results can never erase, or be mistaken for the
    absence of, documentation already found earlier in the same turn.
    """

    def __init__(
        self,
        confluence_service: ConfluenceService | None,
        capture: RetrievalCapture | None = None,
    ) -> None:
        self._service = confluence_service
        self._capture = capture
        self._lock = threading.Lock()
        self._ledger = RetrievalLedger("confluence", item_label="page")
        self._space_keys: dict[str, str] | None = None

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
        normalized_query = re.sub(r"\s+", " ", (query or "").strip())
        effective_space = self._normalize_space_key(space_key)
        try:
            result = self._service.search(  # type: ignore[union-attr]
                query, limit=limit, space_key=effective_space
            )
            pages = parse_confluence_search(result)
            self._ledger.record_search(
                normalized_query,
                [_search_item(page) for page in pages],
                relevance=_relevance_label,
            )
            _capture_search_results(self._capture, result, self._service)
            logger.info(
                "Confluence search_pages completed: %d results returned",
                _count_sources(result),
            )
            logger.info(
                "Confluence search diagnostic: query=%r space=%r results=%d "
                "ids=%r titles=%r parents=%r relevance=%r",
                normalized_query,
                effective_space,
                len(pages),
                [page.get("page_id") for page in pages],
                [page.get("title") for page in pages],
                [page.get("parent") for page in pages],
                [_relevance_label(normalized_query, page) for page in pages],
            )
            logger.debug(
                "Confluence search_pages tool result: %r", self._redact(result)
            )
            return append_state(result, self._ledger.render())
        except ConfluenceApiError as exc:
            self._ledger.record_search_error(normalized_query, str(exc))
            logger.warning("Confluence search failed: %s", exc)
            return append_state(
                f"Confluence search is currently unavailable: {exc}",
                self._ledger.render(),
            )
        except Exception:  # noqa: BLE001
            self._ledger.record_search_error(normalized_query, "unexpected error")
            logger.exception("Unexpected Confluence search failure")
            return append_state(
                "Confluence search is currently unavailable.",
                self._ledger.render(),
            )

    @kernel_function(description=LIST_SPACES_DESCRIPTION, name="list_spaces")
    def list_spaces(self, limit: int = 50) -> str:
        if not self._enabled:
            logger.info("Confluence list_spaces not invoked: Confluence not configured")
            return "Confluence access is not configured for this deployment."
        logger.info("Confluence list_spaces invoked: limit=%r", limit)
        try:
            result = self._service.list_spaces(limit=limit)  # type: ignore[union-attr]
            space_keys = _parse_space_keys(result)
            if space_keys:
                with self._lock:
                    self._space_keys = space_keys
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
            parsed = parse_confluence_page(result)
            if parsed is not None:
                content_length = len(str(parsed.get("content") or ""))
                self._ledger.record_retrieval(
                    page_id,
                    _page_item(page_id, parsed),
                    str(parsed.get("content") or ""),
                )
                logger.info(
                    "Confluence get_page completed: page_id=%r content_length=%d success=true",
                    page_id,
                    content_length,
                )
            else:
                self._ledger.record_content_unavailable(page_id)
                logger.info(
                    "Confluence get_page completed: page_id=%r content_length=0 success=false",
                    page_id,
                )
            _capture_page_result(self._capture, page_id, result)
            return append_state(result, self._ledger.render())
        except ConfluenceApiError as exc:
            self._ledger.record_error(f"page {page_id} retrieval failed: {exc}")
            logger.warning("Confluence get_page(%s) failed: %s", page_id, exc)
            return append_state(
                f"Could not retrieve Confluence page {page_id}: {exc}",
                self._ledger.render(),
            )
        except Exception:  # noqa: BLE001
            self._ledger.record_error(f"page {page_id} retrieval failed: unexpected error")
            logger.exception("Unexpected Confluence get_page failure")
            return append_state(
                f"Could not retrieve Confluence page {page_id}.",
                self._ledger.render(),
            )

    def _known_space_keys(self) -> dict[str, str]:
        """Valid space keys from list_spaces (casefolded -> exact), cached per turn."""
        with self._lock:
            cached = self._space_keys
        if cached is not None:
            return cached
        parsed: dict[str, str] = {}
        try:
            if self._service is not None:
                parsed = _parse_space_keys(self._service.list_spaces())
        except Exception:  # noqa: BLE001 - validation must never break search
            logger.debug("Confluence list_spaces for space validation failed", exc_info=True)
            parsed = {}
        with self._lock:
            self._space_keys = parsed
        return parsed

    def _normalize_space_key(self, space_key: str | None) -> str | None:
        """Return a validated exact space key, or None to search all spaces.

        A space key is only honored when it appears in the ``list_spaces`` response,
        so the model can never narrow a search to an invented/invalid space (which
        would return zero results and look like missing documentation).
        """
        key = (space_key or "").strip()
        if not key:
            return None
        known = self._known_space_keys()
        actual = match_known_scope(key, known)
        if actual is None:
            reason = "no spaces available from list_spaces" if not known else (
                "not returned by list_spaces"
            )
            logger.warning(
                "Confluence ignored space_key=%r (%s); searching all configured spaces",
                key,
                reason,
            )
            return None
        return actual

    @property
    def _enabled(self) -> bool:
        return self._service is not None and self._service.enabled

    @staticmethod
    def _redact(text: str) -> str:
        """Strip anything that looks like a bearer/basic token before logging."""
        import re

        return re.sub(r"(?i)(bearer\s+[A-Za-z0-9._~+/=-]+)", "<redacted>", text or "")


def _capture_search_results(
    capture: RetrievalCapture | None,
    result: str,
    service: ConfluenceService | None = None,
) -> None:
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
        if page.get("owner"):
            metadata["owner"] = page["owner"]
        if page.get("last_editor"):
            metadata["last_editor"] = page["last_editor"]
        _add_recent_editors(
            metadata,
            page.get("recent_editor_details"),
            service,
            str(page_id),
        )
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
    metadata: dict[str, Any] = {}
    if parsed.get("owner"):
        metadata["owner"] = parsed["owner"]
    if parsed.get("last_editor"):
        metadata["last_editor"] = parsed["last_editor"]
    _add_recent_editors(
        metadata,
        parsed.get("recent_editor_details"),
        None,
        page_id,
    )
    draft = ExportItemDraft(
        source_type=SourceType.CONFLUENCE.value,
        source_id=page_id,
        source_name=str(parsed.get("title") or page_id),
        source_url=parsed.get("url"),
        metadata=metadata,
        export_strategy=ScenarioType.GENERATED_DOCUMENT.value,
    )
    if parsed.get("content"):
        draft.merge_content(str(parsed["content"]))
    capture.add(draft)


def _add_recent_editors(
    metadata: dict[str, Any],
    parsed_details: Any,
    service: ConfluenceService | None,
    page_id: str,
) -> None:
    """Attach ordered recent-editor metadata, enriching from version history."""
    details = _normalize_editor_details(parsed_details)
    if not details and service is not None:
        details = _recent_editor_details(service, page_id)
    if details:
        metadata["recent_editor_details"] = details
        metadata["recent_editors"] = [entry["name"] for entry in details]


def _normalize_editor_details(values: Any) -> list[dict[str, str]]:
    details: list[dict[str, str]] = []
    if not isinstance(values, (list, tuple)):
        return details
    for entry in values:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        details.append(
            {
                "name": name,
                "when": str(entry.get("when") or "").strip(),
                "version": str(entry.get("version") or "").strip(),
            }
        )
    return details


def _recent_editor_details(
    service: ConfluenceService | None, page_id: str
) -> list[dict[str, str]]:
    """Best-effort version-history lookup; never raises into the tool loop."""
    try:
        versions_getter = getattr(service, "recent_editor_versions", None)
        if versions_getter is not None:
            details = _normalize_editor_details(versions_getter(page_id))
            if details:
                return details
        names_getter = getattr(service, "recent_editor_names", None)
        if names_getter is not None:
            return [
                {"name": str(name).strip(), "when": "", "version": ""}
                for name in names_getter(page_id) or []
                if str(name).strip()
            ]
    except Exception:  # noqa: BLE001 - capture enrichment must never break retrieval
        logger.debug(
            "Confluence recent-editor capture failed: page_id=%s", page_id, exc_info=True
        )
    return []


def _count_sources(text: str) -> int:
    return text.count("[Source")
