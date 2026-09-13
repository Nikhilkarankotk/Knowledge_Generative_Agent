"""Tests for the Semantic Kernel KnowledgeGenerativeAgent runtime.

Verifies the full function-calling loop over a *scripted* chat service (no network):
tool selection, argument parsing, plugin execution, tool results in history, final
answer production, per-session isolation and the Python 3.14 compatibility patch.
"""

from __future__ import annotations

import pytest
from semantic_kernel.contents import ChatHistory

from app.agents.knowledge_generative_agent import AGENT_NAME, SYSTEM_INSTRUCTIONS
from app.core.config import Settings
from app.core.exceptions import KnowledgeAgentError
from app.models import DocumentChunk
from app.rag.rag_service import RagService
from app.rag.text_chunker import TextChunker
from app.repositories import DocumentChunkRepository
from app.services.mistral_api_service import MistralApiService
from app.sk.compat import apply_py314_compatibility_patch, drop_names_except_tool
from app.sk.semantic_kernel_factory import SemanticKernelFactory
from tests.conftest import FakeLLM
from tests.fake_sk_service import ScriptedChatCompletion, tool_results


class StaticEmbeddingService:
    def __init__(self, query_vector: list[float]) -> None:
        self.query_vector = query_vector

    def generate_embedding(self, text: str) -> list[float]:
        return self.query_vector


class StubConfluence:
    def __init__(self, *, enabled: bool = True, fail: bool = False) -> None:
        self._enabled = enabled
        self._fail = fail
        self.searched: list[str] = []
        self.page_requests: list[str] = []
        self._search_output = (
            "[Source: Confluence: Roadmap (space: Eng)]\n"
            "Page id: 42\n"
            "URL: https://wiki.example.com/spaces/Eng/pages/42\n"
            "Excerpt: Q3 delivery plan"
        )
        self._page_output = (
            "[Source: Confluence: Roadmap]\n"
            "https://wiki.example.com/spaces/Eng/pages/42\n"
            "Q3 ships the payments module."
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def search(self, query: str, limit: int | None = None, space_key: str | None = None) -> str:
        if self._fail:
            from app.core.exceptions import ConfluenceApiError

            raise ConfluenceApiError("Confluence is down")
        self.searched.append(query)
        return self._search_output

    def list_spaces(self, limit: int = 50) -> str:
        return "Available Confluence spaces:\n[Source: Confluence] key=ENG, name=Engineering"

    def get_page(self, page_id: str) -> str:
        self.page_requests.append(page_id)
        return self._page_output

    def close(self) -> None:
        pass


class RecordingRag:
    """RagService stand-in that records calls and returns a fixed context."""

    def __init__(self, context: str = "", empty: bool = False) -> None:
        self._context = context
        self._empty = empty
        self.calls: list[tuple[str, str]] = []

    def is_knowledge_base_empty(self, session_id: str) -> bool:
        return self._empty

    def retrieve_context(self, query: str, session_id: str) -> str:
        self.calls.append((query, session_id))
        return self._context


def make_history(user_message: str) -> ChatHistory:
    history = ChatHistory()
    history.add_user_message(user_message)
    return history


def make_factory(plan, *, answerer=None, delay: float = 0.0) -> tuple[SemanticKernelFactory, ScriptedChatCompletion]:
    fake = ScriptedChatCompletion(plan=plan, answerer=answerer, delay=delay)
    return SemanticKernelFactory(Settings(), chat_service=fake, use_loop=False), fake


def run_turn(
    factory: SemanticKernelFactory,
    *,
    rag=None,
    session_id: str = "s1",
    confluence=None,
    user_message: str = "How do I do this?",
    timeout: float | None = None,
) -> str:
    agent = factory.build_agent(
        rag_service=rag,
        session_id=session_id,
        confluence_service=confluence,
    )
    return factory.run_agent(agent, make_history(user_message), timeout=timeout)


def test_plugins_registered_under_expected_names() -> None:
    factory, _ = make_factory([])
    agent = factory.build_agent(
        rag_service=RecordingRag(context="ctx"),
        confluence_service=StubConfluence(),
    )
    plugins = agent.chat_agent.kernel.plugins
    assert set(plugins.keys()) == {"Knowledge", "Confluence"}
    assert set(plugins["Knowledge"].functions.keys()) == {"search_knowledge"}
    assert set(plugins["Confluence"].functions.keys()) == {
        "search_pages",
        "get_page",
        "list_spaces",
    }
    assert agent.chat_agent.name == AGENT_NAME


def test_build_agent_wires_configured_max_auto_invoke_attempts() -> None:
    settings = Settings()
    factory, _ = make_factory([])
    agent = factory.build_agent(
        rag_service=RecordingRag(context="ctx"),
        confluence_service=StubConfluence(),
    )
    attempts = agent.chat_agent.function_choice_behavior.maximum_auto_invoke_attempts
    assert attempts == settings.sk_max_auto_invoke_attempts
    assert settings.sk_max_auto_invoke_attempts > 0


def test_system_instructions_mandate_automatic_invocation_and_no_permission_asking() -> None:
    assert "authorized to call your tools automatically" in SYSTEM_INSTRUCTIONS
    assert "NEVER ask the user for" in SYSTEM_INSTRUCTIONS
    assert "permission to search a knowledge source" in SYSTEM_INSTRUCTIONS
    assert "invoke ConfluencePlugin immediately" in SYSTEM_INSTRUCTIONS
    assert "Do not claim that Confluence or the uploaded documents contain no information" in (
        SYSTEM_INSTRUCTIONS
    )
    assert "Answer, then stop." in SYSTEM_INSTRUCTIONS
    assert "broaden the search and search again automatically" in SYSTEM_INSTRUCTIONS


def test_system_instructions_have_no_hardcoded_routing_map() -> None:
    assert "->" not in SYSTEM_INSTRUCTIONS
    assert 'if "' not in SYSTEM_INSTRUCTIONS.lower()


def test_routing_architecture_question_invokes_confluence() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "Payments Application architecture"})]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        user_message="What are the main components and architecture of the Payments Application?",
    )

    assert confluence.searched == ["Payments Application architecture"]
    assert "[Source: Confluence: Roadmap" in answer


def test_routing_deployment_question_invokes_confluence() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "Payments application deployment"})]
    )

    run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        user_message="How is the Payments application deployed?",
    )

    assert confluence.searched == ["Payments application deployment"]


def test_routing_api_authentication_question_invokes_confluence() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "Payments API authentication"})]
    )

    run_turn(
        factory,
        rag=RecordingRag(),
        confluence=confluence,
        user_message="How does authentication work for the Payments APIs?",
    )

    assert confluence.searched == ["Payments API authentication"]


def test_routing_uploaded_pdf_question_invokes_knowledge() -> None:
    rag = RecordingRag(context="[Source: report.pdf]\nreport content")
    factory, _ = make_factory(
        [("Knowledge", "search_knowledge", {"query": "what does the uploaded PDF contain"})]
    )

    run_turn(
        factory,
        rag=rag,
        session_id="s1",
        user_message="Summarize my uploaded PDF?",
    )

    assert [query for query, _ in rag.calls] == ["what does the uploaded PDF contain"]


def test_routing_compare_question_invokes_both_tools() -> None:
    rag = RecordingRag(context="[Source: arch.pdf]\narchitecture content")
    confluence = StubConfluence()
    factory, _ = make_factory(
        [
            ("Knowledge", "search_knowledge", {"query": "architecture"}),
            ("Confluence", "search_pages", {"query": "architecture"}),
        ]
    )

    run_turn(
        factory,
        rag=rag,
        confluence=confluence,
        user_message="Compare my uploaded architecture document with the Confluence documentation.",
    )

    assert [query for query, _ in rag.calls] == ["architecture"]
    assert confluence.searched == ["architecture"]


def test_compat_patch_is_idempotent() -> None:
    apply_py314_compatibility_patch()
    apply_py314_compatibility_patch()
    # Rebuilding constructs a ChatCompletionAgent on top of the patched decorator.
    make_factory([])


def test_single_knowledge_tool_call_with_attribution_and_arguments() -> None:
    rag = RecordingRag(context="[Source: payments.pdf]\nthe payment policy is 30 days")
    factory, _ = make_factory([("Knowledge", "search_knowledge", {"query": "payment policy"})])

    answer = run_turn(factory, rag=rag, session_id="s1", user_message="What is the payment policy?")

    # Plugin received the session-bound call with the parsed query argument.
    assert rag.calls == [("payment policy", "s1")]
    # The surfaced tool result preserved attribution and reached the final answer.
    assert "[Source: payments.pdf]" in answer
    assert "30 days" in answer


def test_multi_tool_round_combines_knowledge_and_confluence() -> None:
    rag = RecordingRag(context="[Source: portfolio.md]\nproject summary")
    confluence = StubConfluence()
    factory, fake = make_factory(
        [
            ("Knowledge", "search_knowledge", {"query": "portfolio"}),
            ("Confluence", "search_pages", {"query": "roadmap"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=rag,
        session_id="s1",
        confluence=confluence,
        user_message="Summarize the portfolio against the roadmap.",
    )

    assert "[Source: portfolio.md]" in answer
    assert "[Source: Confluence: Roadmap" in answer
    assert confluence.searched == ["roadmap"]
    assert len(tool_results(fake.histories[-1])) == 2


def test_confluence_get_page_follow_up_receives_page_id() -> None:
    confluence = StubConfluence()
    factory, _ = make_factory(
        [
            ("Confluence", "search_pages", {"query": "roadmap"}),
            ("Confluence", "get_page", {"page_id": "42"}),
        ]
    )

    answer = run_turn(
        factory,
        rag=RecordingRag(),
        session_id="s1",
        confluence=confluence,
        user_message="What is in the roadmap page?",
    )

    assert confluence.searched == ["roadmap"]
    assert confluence.page_requests == ["42"]
    assert "Q3 ships the payments module" in answer


def test_empty_knowledge_base_marker_reaches_final_answer() -> None:
    factory, _ = make_factory([("Knowledge", "search_knowledge", {"query": "x"})])
    answer = run_turn(factory, rag=RecordingRag(empty=True), session_id="s1", user_message="anything")
    assert "No documents have been uploaded" in answer


def test_confluence_failure_becomes_marker_not_exception() -> None:
    confluence = StubConfluence(fail=True)
    factory, _ = make_factory([("Confluence", "search_pages", {"query": "x"})])
    answer = run_turn(
        factory,
        rag=RecordingRag(),
        session_id="s1",
        confluence=confluence,
        user_message="roadmap?",
    )
    assert "currently unavailable" in answer


def test_unconfigured_confluence_returns_marker() -> None:
    factory, _ = make_factory([("Confluence", "search_pages", {"query": "x"})])
    answer = run_turn(factory, rag=RecordingRag(), session_id="s1", user_message="wiki?")
    assert "not configured" in answer


def test_agent_timeout_raises_knowledge_agent_error() -> None:
    factory, _ = make_factory([], delay=0.5)
    with pytest.raises(KnowledgeAgentError, match="timed out"):
        run_turn(factory, rag=RecordingRag(), user_message="slow?", timeout=0.05)


def test_empty_answer_raises_knowledge_agent_error() -> None:
    factory, _ = make_factory([], answerer=lambda history: "   ")
    with pytest.raises(KnowledgeAgentError, match="empty"):
        run_turn(factory, rag=RecordingRag(), user_message="hi")


def test_loop_path_runs_on_shared_background_loop() -> None:
    """Production path: agents run on the process-wide asyncio loop thread."""
    fake = ScriptedChatCompletion(
        plan=[("Knowledge", "search_knowledge", {"query": "x"})],
        answerer=lambda history: "answer from loop",
    )
    factory = SemanticKernelFactory(Settings(), chat_service=fake, use_loop=True)
    try:
        assert factory._loop is not None and factory._loop_thread is not None  # type: ignore[attr-defined]
        answer = run_turn(factory, rag=RecordingRag(context="ctx"), session_id="s1", user_message="q")
        assert answer == "answer from loop"
        # The model call executed on the background loop thread.
        assert len(fake.histories) == 2
    finally:
        factory.close()
    assert factory._loop is None  # type: ignore[attr-defined]


def test_session_isolation_between_sessions(db_session) -> None:
    """Two sessions with different documents must never leak context."""
    rag = _real_rag(db_session)
    save_chunk(db_session, "session-a", "[Source: payments.pdf]\ncredit card fees are 3%", 10.0)
    save_chunk(db_session, "session-b", "[Source: hiring.md]\nvacation policy is 25 days", 20.0)

    factory, _ = make_factory([("Knowledge", "search_knowledge", {"query": "policy"})])

    answer_a = run_turn(factory, rag=rag, session_id="session-a", user_message="policy?")
    answer_b = run_turn(factory, rag=rag, session_id="session-b", user_message="policy?")

    assert "[Source: payments.pdf]" in answer_a
    assert "credit card fees are 3%" in answer_a
    assert "[Source: payments.pdf]" not in answer_b
    assert "[Source: hiring.md]" in answer_b
    assert "vacation policy is 25 days" in answer_b


def _real_rag(db_session) -> RagService:
    return RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0, 0.0]),
        chunk_repo=DocumentChunkRepository(db_session),
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        top_k=5,
    )


def save_chunk(db_session, session_id: str, text: str, vector_value: float) -> None:
    repo = DocumentChunkRepository(db_session)
    repo.save(
        DocumentChunk(
            text=text,
            session_id=session_id,
            source_filename="doc.txt",
            embedding=[vector_value, 0.0, 0.0],
        )
    )


def test_drop_names_except_tool_strips_message_names() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "instr", "name": "KnowledgeGenerativeAgent"},
        {"role": "user", "content": "hi", "name": "someone"},
        {"role": "assistant", "content": None, "name": "KnowledgeGenerativeAgent", "tool_calls": []},
        {"role": "tool", "tool_call_id": "1", "name": "Confluence-search_pages", "content": "[]"},
    ]
    drop_names_except_tool(messages)
    assert messages[0] == {"role": "system", "content": "instr"}
    assert messages[1] == {"role": "user", "content": "hi"}
    assert messages[2] == {"role": "assistant", "content": None, "tool_calls": []}
    assert messages[3]["name"] == "Confluence-search_pages"


def test_drop_names_except_tool_keeps_tool_names() -> None:
    messages: list[dict[str, object]] = [
        {"role": "tool", "tool_call_id": "9", "name": "Knowledge-search_documents", "content": "[]"}
    ]
    assert drop_names_except_tool(messages)[0]["name"] == "Knowledge-search_documents"
