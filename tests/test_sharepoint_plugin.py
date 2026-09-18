"""Tests for :mod:`app.plugins.sharepoint_plugin` (SharePoint-backed tools)."""

from __future__ import annotations

import logging

from app.plugins.sharepoint_plugin import (
    GET_SHAREPOINT_DOCUMENT_DESCRIPTION,
    LIST_SHAREPOINT_DOCUMENTS_DESCRIPTION,
    SEARCH_SHAREPOINT_CONTENT_DESCRIPTION,
    SEARCH_SHAREPOINT_DESCRIPTION,
    SharePointPlugin,
)

SITE = "knowledgegenagent.sharepoint.com,site,web"


class StubSharePoint:
    def __init__(self) -> None:
        self.enabled = True
        self.allowed_sites = [SITE]
        self.search_args: list[tuple[str, int | None]] = []
        self.search_result = "[Source: SharePoint: content-boundaries.md]\nURL: https://x"
        self.list_result = "[Source: SharePoint: site kga]\nDocument library drive id: b!drive"

    def search(self, query: str, limit: int | None = None) -> str:
        self.search_args.append((query, limit))
        return self.search_result

    def search_file_content(self, query: str, limit: int | None = None) -> str:
        return f"[Source: SharePoint: content-boundaries.md]\n{query}"

    def list_files(self, limit: int | None = None) -> str:
        self.list_args = (limit,)
        return self.list_result

    def get_document_content(self, document_id: str, drive_id: str) -> str:
        self.content_args = (document_id, drive_id)
        return f"[Source: SharePoint: doc]\n{drive_id} {document_id}"


class FailSharePoint(StubSharePoint):
    def search(self, query: str, limit: int | None = None) -> str:  # type: ignore[override]
        raise RuntimeError("boom")

    def list_files(self, limit: int | None = None) -> str:  # type: ignore[override]
        raise RuntimeError("boom")

    def get_document_content(self, document_id: str, drive_id: str) -> str:  # type: ignore[override]
        raise RuntimeError("boom")


def test_search_sharepoint_forwards_query_and_limit() -> None:
    stub = StubSharePoint()
    plugin = SharePointPlugin(stub)  # type: ignore[arg-type]

    result = plugin.search_sharepoint("onboarding process", limit=3)

    assert result == stub.search_result
    assert stub.search_args == [("onboarding process", 3)]


def test_search_sharepoint_forwards_query_without_limit() -> None:
    stub = StubSharePoint()
    plugin = SharePointPlugin(stub)  # type: ignore[arg-type]

    plugin.search_sharepoint("vacation policy")

    assert stub.search_args == [("vacation policy", None)]


def test_plugin_exposes_no_folder_or_site_arguments() -> None:
    """The LLM must not be able to steer retrieval toward any folder/site."""
    plugin = SharePointPlugin(StubSharePoint())  # type: ignore[arg-type]
    for name in ("search_sharepoint", "search_sharepoint_content",
                 "list_sharepoint_documents", "get_sharepoint_document"):
        for param in ("folder", "site_id", "folder_id", "path", "drive_root"):
            assert param not in (
                p for p in __import__("inspect").signature(getattr(plugin, name)).parameters
            ), f"{name} must not expose parameter {param!r}"


def test_list_sharepoint_documents_forwards_limit() -> None:
    stub = StubSharePoint()
    plugin = SharePointPlugin(stub)  # type: ignore[arg-type]

    result = plugin.list_sharepoint_documents(limit=5)

    assert result == stub.list_result
    assert stub.list_args == (5,)


def test_search_sharepoint_content_forwards_query_and_limit() -> None:
    stub = StubSharePoint()
    plugin = SharePointPlugin(stub)  # type: ignore[arg-type]

    result = plugin.search_sharepoint_content("vacation policy", limit=2)

    assert "vacation policy" in result
    assert stub.search_args == []


def test_get_sharepoint_document_forwards_ids() -> None:
    stub = StubSharePoint()
    plugin = SharePointPlugin(stub)  # type: ignore[arg-type]

    result = plugin.get_sharepoint_document("01onboard", "b!drive")

    assert "b!drive" in result
    assert "01onboard" in result
    assert stub.content_args == ("01onboard", "b!drive")


def test_search_hits_are_candidates_and_only_a_read_document_is_exportable() -> None:
    """A search returning several PDFs must not make them all exportable; only a
    document the agent opens with get_sharepoint_document is a source by itself.
    (Relevance promotion at end of turn handles the search-only case.)"""
    from app.export.capture import RetrievalCapture

    stub = StubSharePoint()
    stub.search_result = (
        "[Source: SharePoint: Job_Portal_Web_Application CICD Pipeline flow.pdf]\n"
        "URL: https://sp/jp.pdf\nDrive id: d1\nDocument id: JP\nSize: 10 bytes\n\n"
        "[Source: SharePoint: n8n CICD Pipeline flow.pdf]\n"
        "URL: https://sp/n8n.pdf\nDrive id: d1\nDocument id: N8\nSize: 10 bytes"
    )
    capture = RetrievalCapture()
    plugin = SharePointPlugin(stub, capture=capture)  # type: ignore[arg-type]

    plugin.search_sharepoint("pipeline")
    assert {i.source_id: i.exportable for i in capture.items} == {"JP": False, "N8": False}

    stub.get_document_content = lambda document_id, drive_id: (  # type: ignore[method-assign]
        "[Source: SharePoint: Job_Portal_Web_Application CICD Pipeline flow.pdf]\n"
        "URL: https://sp/jp.pdf\nDrive id: d1\nDocument id: JP\nfull text"
    )
    plugin.get_sharepoint_document("JP", "d1")
    assert {i.source_id: i.exportable for i in capture.items} == {"JP": True, "N8": False}


def test_disabled_sharepoint_returns_marker() -> None:
    class DisabledStub(StubSharePoint):
        def __init__(self) -> None:
            super().__init__()
            self.enabled = False

    plugin = SharePointPlugin(DisabledStub())  # type: ignore[arg-type]
    assert "not configured" in plugin.search_sharepoint("billing")
    assert "not configured" in plugin.search_sharepoint_content("billing")
    assert "not configured" in plugin.list_sharepoint_documents()
    assert "not configured" in plugin.get_sharepoint_document("1", "d")


def test_unavailable_sharepoint_returns_marker_not_exception() -> None:
    plugin = SharePointPlugin(FailSharePoint())  # type: ignore[arg-type]
    assert "SharePoint search is currently unavailable" in plugin.search_sharepoint("billing")
    assert "Could not list SharePoint documents" in plugin.list_sharepoint_documents()
    assert "Could not retrieve SharePoint document 1" in plugin.get_sharepoint_document("1", "d")


def test_plugin_exposes_no_raw_graph_or_site_selection_functions() -> None:
    plugin = SharePointPlugin(StubSharePoint())  # type: ignore[arg-type]
    functions = {
        name
        for name in dir(plugin)
        if not name.startswith("_") and callable(getattr(plugin, name))
    }
    assert {"search_sharepoint", "search_sharepoint_content", "list_sharepoint_documents",
            "get_sharepoint_document"} <= functions
    assert not any(
        "graph" in name or "execute" in name or "url" in name or "raw" in name
        for name in functions
    )


def test_sharepoint_descriptions_cover_folder_scope_and_auto_invoke() -> None:
    search = SEARCH_SHAREPOINT_DESCRIPTION.lower()
    for term in (
        "enterprise documents",
        "application documentation",
        "architecture",
        "onboarding",
        "internal policies",
        "configured",
        "knowledge-base folder",
        "cannot pass a folder",
        "automatically",
        "without asking the user for permission",
    ):
        assert term in search, f"missing concept: {term}"
    assert "Payments" not in SEARCH_SHAREPOINT_DESCRIPTION
    assert "search_pages" not in SEARCH_SHAREPOINT_DESCRIPTION

    list_desc = LIST_SHAREPOINT_DOCUMENTS_DESCRIPTION.lower()
    assert "list" in list_desc
    assert "knowledge-base folder" in list_desc
    assert "drive and item ids" in list_desc

    content = SEARCH_SHAREPOINT_CONTENT_DESCRIPTION.lower()
    assert "content" in content
    assert "search" in content
    assert "knowledge-base folder" in content

    get = GET_SHAREPOINT_DOCUMENT_DESCRIPTION.lower()
    assert "drive id" in get
    assert "document id" in get
    assert "knowledge-base" in get


def test_search_sharepoint_logs_invocation_and_result_count(caplog) -> None:
    plugin = SharePointPlugin(StubSharePoint())  # type: ignore[arg-type]
    with caplog.at_level(logging.INFO, logger="app.plugins.sharepoint_plugin"):
        plugin.search_sharepoint("onboarding", limit=2)

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "search_sharepoint invoked: query='onboarding' limit=2" in msg
        for msg in messages
    )
    assert any("completed: 1 results returned" in msg for msg in messages)
