"""Tests for :mod:`app.plugins.confluence_plugin` (Confluence-backed tools)."""

from __future__ import annotations

import logging

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

    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:
        self.search_args.append((query, limit, space_key))
        return self.search_result

    def list_spaces(self, limit: int = 50) -> str:
        return self.spaces_result

    def get_page(self, page_id: str) -> str:
        return f"[Source: Confluence: Page {page_id}]"


class FailConfluence(StubConfluence):
    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:  # type: ignore[override]
        raise RuntimeError("boom")

    def list_spaces(self, limit: int = 50) -> str:  # type: ignore[override]
        raise RuntimeError("boom")


def test_search_pages_forwards_query_and_scoping() -> None:
    stub = StubConfluence()
    plugin = ConfluencePlugin(stub)  # type: ignore[arg-type]

    result = plugin.search_pages("payments architecture", limit=3, space_key="PAY")

    assert result == stub.search_result
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
