"""Tests for :mod:`app.plugins.confluence_plugin` (Confluence-backed tools)."""

from __future__ import annotations

import logging

from app.core.exceptions import ConfluenceApiError
from app.plugins.confluence_plugin import (
    GET_PAGE_DESCRIPTION,
    LIST_SPACES_DESCRIPTION,
    SEARCH_PAGES_DESCRIPTION,
    ConfluencePlugin,
)


class StubConfluence:
    def __init__(self) -> None:
        self.enabled = True
        self.search_args: list[tuple[str, int | None, str | None]] = []
        self.search_result = "[Source: Confluence: Billing 101]\nURL: https://x"
        self.spaces_result = "Available Confluence spaces:\n[Source: Confluence] key=PAY, name=Payments"
        self.page_result: str | None = None
        self.recent_editors: list[str] = []
        self.recent_editor_details: list[dict[str, str]] = []

    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:
        self.search_args.append((query, limit, space_key))
        return self.search_result

    def list_spaces(self, limit: int = 50) -> str:
        return self.spaces_result

    def get_page(self, page_id: str) -> str:
        return self.page_result or f"[Source: Confluence: Page {page_id}]"

    def recent_editor_names(self, page_id: str) -> list[str]:
        return list(self.recent_editors)

    def recent_editor_versions(self, page_id: str) -> list[dict[str, str]]:
        return [dict(entry) for entry in self.recent_editor_details]


class FailConfluence(StubConfluence):
    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:  # type: ignore[override]
        raise RuntimeError("boom")

    def list_spaces(self, limit: int = 50) -> str:  # type: ignore[override]
        raise RuntimeError("boom")


class ScriptedConfluence:
    """Service stub with per-query results, per-page content and failure modes."""

    enabled = True

    def __init__(
        self,
        results_by_query: dict[str, str] | None = None,
        *,
        fail_queries: set[str] | None = None,
        pages: dict[str, str] | None = None,
        spaces: str = (
            "Available Confluence spaces:\n"
            "[Source: Confluence] key=ARCH, name=Architecture"
        ),
    ) -> None:
        self.calls: list[tuple[str, int | None, str | None]] = []
        self.page_requests: list[str] = []
        self._results = dict(results_by_query or {})
        self._fail_queries = set(fail_queries or ())
        self._pages = dict(pages or {})
        self._spaces = spaces

    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:
        self.calls.append((query, limit, space_key))
        if query in self._fail_queries:
            raise ConfluenceApiError("Confluence search is down")
        return self._results.get(
            query,
            "No Confluence pages matched this query. This is not proof that "
            "Confluence has no relevant documentation.",
        )

    def list_spaces(self, limit: int = 50) -> str:
        return self._spaces

    def get_page(self, page_id: str) -> str:
        self.page_requests.append(page_id)
        return self._pages.get(
            page_id, f"No content available for Confluence page '{page_id}'."
        )


def _search_block(
    page_id: str,
    title: str,
    *,
    space: str = "ARCH",
    parent: str | None = None,
    excerpt: str = "design details",
) -> str:
    lines = [
        f"[Source: Confluence: {title}] (space: {space})",
        f"Page id: {page_id}",
        f"URL: https://wiki.example.com/spaces/{space}/pages/{page_id}",
    ]
    if parent:
        lines.append(f"Parent: {parent}")
    lines.append(f"Excerpt: {excerpt}")
    return "\n".join(lines)


def test_search_pages_forwards_query_and_scoping() -> None:
    stub = StubConfluence()
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    result = plugin.search_pages("payments architecture", limit=3, space_key="PAY")

    assert result.startswith(stub.search_result)
    assert "CONFLUENCE RETRIEVAL STATE" in result
    assert stub.search_args == [("payments architecture", 3, "PAY")]


def test_search_pages_without_space_scoping() -> None:
    stub = StubConfluence()
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    plugin.search_pages("billing")

    assert stub.search_args == [("billing", None, None)]


def test_list_spaces_returns_spaces() -> None:
    stub = StubConfluence()
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    result = plugin.list_spaces()

    assert result == stub.spaces_result


def test_disabled_confluence_returns_marker() -> None:
    class DisabledStub(StubConfluence):
        def __init__(self) -> None:
            super().__init__()
            self.enabled = False

    plugin = ConfluencePlugin(DisabledStub())  # type: ignore[arg-type]
    assert "not configured" in plugin.search_pages("billing")
    assert "not configured" in plugin.list_spaces()
    assert "not configured" in plugin.get_page("1")


def test_unavailable_confluence_returns_marker_not_exception() -> None:
    plugin = ConfluencePlugin(FailConfluence())  # type: ignore[arg-type]
    assert "search is currently unavailable" in plugin.search_pages("billing")
    assert "spaces are currently unavailable" in plugin.list_spaces()


def test_search_pages_description_covers_technical_concepts_and_auto_invoke() -> None:
    description = SEARCH_PAGES_DESCRIPTION.lower()
    for term in (
        "application",
        "service",
        "architecture",
        "system design",
        "api",
        "deployment",
        "security",
        "engineering",
        "runbook",
        "project documentation",
    ):
        assert term in description, f"missing concept: {term}"
    assert "automatically" in description
    assert "without asking the user for permission" in description
    assert "information" in description
    assert "Payments" not in SEARCH_PAGES_DESCRIPTION


def test_list_spaces_description_mentions_scoping_and_auto_invoke() -> None:
    description = LIST_SPACES_DESCRIPTION.lower()
    assert "space_key" in description
    assert "automatically" in description
    assert "page id" in GET_PAGE_DESCRIPTION.lower()
    assert "search_pages" in GET_PAGE_DESCRIPTION.lower()


def test_search_pages_logs_invocation_and_result_count(caplog) -> None:
    plugin = ConfluencePlugin(StubConfluence())  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="app.plugins.confluence_plugin"):
        plugin.search_pages("billing", limit=3, space_key="PAY")

    messages = [record.getMessage() for record in caplog.records]
    assert any("search_pages invoked: query='billing' space_key='PAY' limit=3" in msg for msg in messages)
    assert any("completed: 1 results returned" in msg for msg in messages)


def test_search_pages_capture_includes_space_and_parent() -> None:
    from app.export.capture import RetrievalCapture

    stub = StubConfluence()
    stub.search_result = (
        "[Source: Confluence: API Documentation (space: PAY)]\n"
        "Page id: DOC1\n"
        "URL: https://wiki.example.com/spaces/PAY/pages/DOC1\n"
        "Parent: Payments Application\n"
        "Excerpt: REST endpoint reference"
    )
    capture = RetrievalCapture()
    plugin = ConfluencePlugin(stub, capture=capture)  # type: ignore[arg-type]

    plugin.search_pages("Payments application API documentation")

    assert capture.items
    item = capture.items[0]
    assert item.source_type == "CONFLUENCE"
    assert item.source_id == "DOC1"
    assert item.source_name == "API Documentation"
    assert item.source_url == "https://wiki.example.com/spaces/PAY/pages/DOC1"
    assert item.metadata.get("space") == "PAY"
    assert item.metadata.get("parent") == "Payments Application"
    assert "REST endpoint reference" in (item.content_reference or "")


def test_search_pages_capture_includes_recent_editors() -> None:
    from app.export.capture import RetrievalCapture

    stub = StubConfluence()
    stub.search_result = (
        "[Source: Confluence: Netflix System Design (space: ARCH)]\n"
        "Page id: 9000\n"
        "URL: https://wiki.example.com/spaces/ARCH/pages/9000\n"
        "Owner: Nikhil Karankot\n"
        "Last modified by: Tejaswinik\n"
        "Excerpt: design"
    )
    stub.recent_editor_details = [
        {"name": "Tejaswinik", "when": "2026-02-02T10:00:00Z", "version": "2"},
        {"name": "Nikhil Karankot", "when": "2026-01-01T10:00:00Z", "version": "1"},
    ]
    capture = RetrievalCapture()
    plugin = ConfluencePlugin(stub, capture=capture)  # type: ignore[arg-type]

    plugin.search_pages("Netflix system design")

    item = capture.items[0]
    assert item.metadata.get("owner") == "Nikhil Karankot"
    assert item.metadata.get("last_editor") == "Tejaswinik"
    assert item.metadata.get("recent_editors") == ["Tejaswinik", "Nikhil Karankot"]
    assert item.metadata.get("recent_editor_details") == stub.recent_editor_details


def test_get_page_capture_includes_recent_editors() -> None:
    from app.export.capture import RetrievalCapture

    stub = StubConfluence()
    stub.page_result = (
        "[Source: Confluence: Netflix System Design]\n"
        "Owner: Nikhil Karankot\n"
        "Last modified by: Tejaswinik\n"
        "Recent editor: Tejaswinik | 2026-02-02T10:00:00Z | 2\n"
        "Recent editor: Nikhil Karankot | 2026-01-01T10:00:00Z | 1\n"
        "https://wiki.example.com/spaces/ARCH/pages/9000\n"
        "Design content"
    )
    capture = RetrievalCapture()
    plugin = ConfluencePlugin(stub, capture=capture)  # type: ignore[arg-type]

    plugin.get_page("9000")

    item = capture.items[0]
    assert item.source_id == "9000"
    assert item.metadata.get("owner") == "Nikhil Karankot"
    assert item.metadata.get("last_editor") == "Tejaswinik"
    assert item.metadata.get("recent_editors") == ["Tejaswinik", "Nikhil Karankot"]
    assert item.metadata.get("recent_editor_details") == [
        {"name": "Tejaswinik", "when": "2026-02-02T10:00:00Z", "version": "2"},
        {"name": "Nikhil Karankot", "when": "2026-01-01T10:00:00Z", "version": "1"},
    ]


def test_search_evidence_survives_later_empty_search() -> None:
    """Scenario 3: a later empty search must not erase an earlier result."""
    page = _search_block("9000", "Netflix System Design and Implementation", parent="System Design")
    stub = ScriptedConfluence({"netflix": page})
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    first = plugin.search_pages("netflix")
    second = plugin.search_pages("netflix architecture")

    assert "Netflix System Design and Implementation" in first
    assert "Netflix System Design and Implementation" in second
    assert '"last_search_results": 0' in second
    assert '"evidence_found": true' in second
    assert "does not erase them" in second


def test_invalid_space_key_is_dropped_and_search_broadened() -> None:
    """Scenario 8: a space key not returned by list_spaces is never used."""
    page = _search_block("1", "Netflix System Design")
    stub = ScriptedConfluence({"netflix": page})
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    plugin.search_pages("netflix", space_key="MFS")

    assert stub.calls == [("netflix", None, None)]


def test_valid_space_key_is_normalized_to_exact_value() -> None:
    page = _search_block("1", "Netflix System Design")
    stub = ScriptedConfluence({"netflix": page})
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    plugin.search_pages("netflix", space_key="arch")

    assert stub.calls == [("netflix", None, "ARCH")]


def test_search_failure_preserves_prior_evidence_and_reports_state() -> None:
    """Scenario 6: an API failure must not discard earlier evidence."""
    page = _search_block("9000", "Netflix System Design")
    stub = ScriptedConfluence(
        {"netflix": page}, fail_queries={"netflix architecture"}
    )
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    plugin.search_pages("netflix")
    result = plugin.search_pages("netflix architecture")

    assert "currently unavailable" in result
    assert "Netflix System Design" in result
    assert '"evidence_found": true' in result
    assert "search failed" in result


def test_get_page_marks_evidence_retrieved_in_state() -> None:
    page = _search_block("9000", "Netflix System Design")
    stub = ScriptedConfluence(
        {"netflix": page},
        pages={
            "9000": (
                "[Source: Confluence: Netflix System Design]\n"
                "URL: https://wiki.example.com/spaces/ARCH/pages/9000\n"
                "Actual design content"
            )
        },
    )
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    plugin.search_pages("netflix")
    result = plugin.get_page("9000")

    assert "Actual design content" in result
    assert '"retrieved": true' in result
    assert '"content_length": 21' in result
    assert stub.page_requests == ["9000"]


def test_empty_search_state_does_not_claim_absence() -> None:
    """Scenario 7: zero results is not proof that documentation does not exist."""
    stub = ScriptedConfluence()
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    result = plugin.search_pages("unknown topic")

    assert '"evidence_found": false' in result
    assert "not yet" in result
    assert "proof" in result


def test_retrieval_state_never_leaks_into_capture() -> None:
    from app.export.capture import RetrievalCapture

    page = _search_block("DOC1", "API Documentation", parent="Payments Application")
    stub = ScriptedConfluence({"api": page})
    capture = RetrievalCapture()
    plugin = ConfluencePlugin(stub, capture=capture)  # type: ignore[arg-type]

    result = plugin.search_pages("api")

    assert "CONFLUENCE RETRIEVAL STATE" in result
    assert len(capture.items) == 1
    assert "CONFLUENCE RETRIEVAL STATE" not in (capture.items[0].content_reference or "")
