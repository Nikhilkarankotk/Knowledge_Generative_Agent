"""Integration tests: ChatService routing through the Knowledge Generative Agent.

Verifies that when a ``SemanticKernelFactory`` is wired in, conversations flow through
the agent, PostgreSQL ``chat_message`` stays the source of truth (history is rebuilt
from the DB each turn), and agent failures transparently fall back to the legacy
augmented-prompt path.
"""

from __future__ import annotations

from types import SimpleNamespace

from semantic_kernel.contents import AuthorRole

from app.core.config import Settings
from app.models import ChatMessage, DocumentChunk
from app.rag.rag_service import RagService
from app.rag.text_chunker import TextChunker
from app.repositories import ChatMessageRepository, DocumentChunkRepository
from app.services.chat_service import ChatService
from app.services.conversation_memory_service import ConversationMemoryService
from app.services.mistral_api_service import MistralApiService
from app.services.translation_service import TranslationService
from app.sk.semantic_kernel_factory import SemanticKernelFactory
from app.sk.source_planner import NoOpSourcePlanner
from tests.conftest import FakeLLM
from tests.fake_sk_service import ScriptedChatCompletion, tool_results


class RecordingRag:
    def __init__(self, context: str = "", empty: bool = False) -> None:
        self._context = context
        self._empty = empty

    def is_knowledge_base_empty(self, session_id: str) -> bool:
        return self._empty

    def retrieve_context(self, query: str, session_id: str) -> str:
        return self._context

    def ingest_document(self, content: bytes, filename: str | None, session_id: str) -> None:
        pass

    def delete_session(self, session_id: str) -> None:
        pass


class StaticEmbeddingService:
    def generate_embedding(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def make_service(db_session, factory: SemanticKernelFactory | None, rag, *, llm: FakeLLM | None = None) -> ChatService:
    llm = llm or FakeLLM()
    mistral = MistralApiService(llm)
    return ChatService(
        chat_repo=ChatMessageRepository(db_session),
        mistral_service=mistral,
        memory_service=ConversationMemoryService(max_history=10),
        translation_service=TranslationService(mistral),
        rag_service=rag,
        semantic_kernel_factory=factory,
        max_history=20,
    )


def make_factory(plan, *, answerer=None):
    fake = ScriptedChatCompletion(plan=plan, answerer=answerer)
    return (
        SemanticKernelFactory(
            Settings(),
            chat_service=fake,
            source_planner=NoOpSourcePlanner(),
            use_loop=False,
        ),
        fake,
    )


def test_agent_path_persists_answer_and_skips_legacy_call(db_session) -> None:
    llm = FakeLLM()
    factory, _ = make_factory([], answerer=lambda history: "Agent answer")
    service = make_service(db_session, factory, RecordingRag(), llm=llm)

    reply = service.process_user_message("s1", "hello")

    assert reply.role == "assistant"
    assert reply.content == "Agent answer"
    assert llm.chat_requests == []

    users = db_session.query(ChatMessage).filter_by(session_id="s1", role="user").all()
    assistants = db_session.query(ChatMessage).filter_by(session_id="s1", role="assistant").all()
    assert [m.content for m in users] == ["hello"]
    assert [m.content for m in assistants] == ["Agent answer"]

    context = service._memory_service.get_context("s1")
    assert context[0]["user"] == "hello"
    assert context[0]["assistant"] == "Agent answer"


def test_agent_confluence_retrieval_persists_export_context(db_session) -> None:
    """The agent path must persist the captured Confluence source (including the
    nested-page parent) into the export context, surviving end-to-end."""
    from app.export.formats import ScenarioType
    from app.repositories import ExportContextRepository

    class StubConfluence:
        enabled = True

        def search(self, query, limit=None, space_key=None):
            return (
                "[Source: Confluence: API Documentation (space: PAY)]\n"
                "Page id: DOC1\n"
                "URL: https://wiki.example.com/spaces/PAY/pages/DOC1\n"
                "Parent: Payments Application\n"
                "Excerpt: REST endpoint reference for the Payments APIs."
            )

    factory, _ = make_factory(
        [("Confluence", "search_pages", {"query": "Payments application API documentation"})]
    )
    mistral = MistralApiService(FakeLLM())
    service = ChatService(
        chat_repo=ChatMessageRepository(db_session),
        mistral_service=mistral,
        memory_service=ConversationMemoryService(max_history=10),
        translation_service=TranslationService(mistral),
        rag_service=RecordingRag(),
        semantic_kernel_factory=factory,
        confluence_service=StubConfluence(),
        export_repo=ExportContextRepository(db_session),
        settings=SimpleNamespace(export_context_ttl_days=30),
    )

    reply = service.process_user_message("s5", "Where is the API documentation?")

    items = ExportContextRepository(db_session).find_items_by_chat_message_id(reply.id)
    assert len(items) == 1
    assert items[0].source_type == "CONFLUENCE"
    assert items[0].source_id == "DOC1"
    assert items[0].export_strategy == ScenarioType.GENERATED_DOCUMENT.value
    assert items[0].meta == {"space": "PAY", "parent": "Payments Application"}


def test_agent_history_is_rebuilt_from_db_each_turn(db_session) -> None:
    factory, fake = make_factory(
        [("Knowledge", "search_knowledge", {"query": "x"})],
        answerer=lambda history: "Final: " + "\n".join(tool_results(history)),
    )
    service = make_service(
        db_session,
        factory,
        RecordingRag(context="[Source: notes.md]\nprior answer content"),
    )

    reply1 = service.process_user_message("s1", "first turn")
    assert reply1.content.startswith("Final:")

    reply2 = service.process_user_message("s1", "second turn")
    assert reply2.content.startswith("Final:")

    # The third recorded chat history snapshot belongs to the second turn and must
    # contain the assistant answer persisted from the first turn.
    assert len(fake.histories) >= 3
    second_turn_snapshot = fake.histories[2]
    prior_answers = [
        message.content
        for message in second_turn_snapshot.messages
        if message.role == AuthorRole.ASSISTANT and message.content
    ]
    assert reply1.content in prior_answers


def test_agent_failure_falls_back_to_legacy_prompt_path(db_session) -> None:
    def boom(history):
        raise RuntimeError("model exploded")

    factory, _ = make_factory([], answerer=boom)
    llm = FakeLLM()
    service = make_service(db_session, factory, RecordingRag(), llm=llm)

    reply = service.process_user_message("s1", "hello")

    # The fallback path answered via the augmented prompt and the fake Mistral.
    assert reply.content == "Test assistant response"
    assert len(llm.chat_requests) == 1
    assert llm.chat_requests[0].endswith("User: hello")

    # Only one user message row was ever saved (not duplicated by the fallback).
    users = db_session.query(ChatMessage).filter_by(session_id="s1", role="user").all()
    assert [m.content for m in users] == ["hello"]


def test_session_isolation_through_chat_service(db_session) -> None:
    rag = _real_rag(db_session)
    save_chunk(db_session, "sA", "[Source: payments.pdf]\ncredit card fees are 3%", 10.0)
    save_chunk(db_session, "sB", "[Source: hiring.md]\nvacation policy is 25 days", 20.0)

    factory, _ = make_factory(
        [("Knowledge", "search_knowledge", {"query": "policy"})],
        answerer=lambda history: "\n".join(tool_results(history)),
    )
    service = make_service(db_session, factory, rag)

    reply_a = service.process_user_message("sA", "what is the policy?")
    reply_b = service.process_user_message("sB", "what is the policy?")

    assert "credit card fees are 3%" in reply_a.content
    assert "[Source: payments.pdf]" in reply_a.content
    assert "vacation policy is 25 days" in reply_b.content
    assert "[Source: payments.pdf]" not in reply_b.content


def test_api_chat_routes_through_knowledge_agent(client, api_llm, db_session) -> None:
    from app.api import dependencies as deps
    from app.main import app as fastapi_app

    rag = _real_rag(db_session)
    factory, _ = make_factory([], answerer=lambda history: "Agent API answer")
    service = make_service(db_session, factory, rag, llm=api_llm)

    fastapi_app.dependency_overrides[deps.get_chat_service] = lambda: service
    try:
        response = client.post(
            "/api/chat",
            json={"message": "hi"},
            headers={"X-Session-ID": "s9"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["content"] == "Agent API answer"
        assert body["sessionId"] == "s9"
        assert api_llm.chat_requests == []
    finally:
        fastapi_app.dependency_overrides.clear()


def _real_rag(db_session) -> RagService:
    return RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService(),
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
