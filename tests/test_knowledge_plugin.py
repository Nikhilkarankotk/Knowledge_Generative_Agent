"""Tests for :mod:`app.plugins.knowledge_plugin` (RAG-backed tool)."""

from __future__ import annotations

from app.plugins.knowledge_plugin import SEARCH_KNOWLEDGE_DESCRIPTION, KnowledgePlugin


class StubRag:
    def __init__(self, *, empty: bool = False, context: str = "", fail: bool = False) -> None:
        self._empty = empty
        self._context = context
        self._fail = fail
        self.queries: list[tuple[str, str]] = []

    def is_knowledge_base_empty(self, session_id: str) -> bool:
        return self._empty

    def retrieve_context(self, query: str, session_id: str) -> str:
        self.queries.append((query, session_id))
        if self._fail:
            raise RuntimeError("chunk repo exploded")
        return self._context


def test_returns_retrieved_context_with_session_binding() -> None:
    rag = StubRag(context="[Source: payments.pdf]\nthe payment policy is 30 days")
    plugin = KnowledgePlugin(rag, "session-9")

    result = plugin.search_knowledge("payment policy")

    assert result == "[Source: payments.pdf]\nthe payment policy is 30 days"
    assert rag.queries == [("payment policy", "session-9")]


def test_empty_knowledge_base_returns_marker() -> None:
    plugin = KnowledgePlugin(StubRag(empty=True), "s1")
    result = plugin.search_knowledge("anything")
    assert "No documents have been uploaded" in result


def test_no_matching_context_returns_marker() -> None:
    plugin = KnowledgePlugin(StubRag(context="   "), "s1")
    result = plugin.search_knowledge("anything")
    assert "No relevant documents found" in result


def test_retrieval_failure_returns_marker_not_exception() -> None:
    plugin = KnowledgePlugin(StubRag(fail=True), "s1")
    result = plugin.search_knowledge("anything")
    assert "could not be searched" in result


def test_plugin_requires_service() -> None:
    try:
        KnowledgePlugin(None, "s1")  # type: ignore[arg-type]
    except ValueError as exc:
        assert "rag_service is required" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_search_knowledge_description_distinguishes_session_documents() -> None:
    description = SEARCH_KNOWLEDGE_DESCRIPTION.lower()
    assert "uploaded documents" in description
    assert "current chat session" in description
    assert "pdfs" in description and "docx" in description
    assert "confluence" in description
    assert "is different from" in SEARCH_KNOWLEDGE_DESCRIPTION.lower()
