"""Tests for :mod:`app.services.chat_service` (ChatService.java).

Focus: preservation of the exact augmented-prompt construction, the message persistence
behaviour (user message has no timestamp/language fields, assistant message has them),
and the exchange stored in conversation memory.
"""


from app.models import ChatMessage
from app.repositories import ChatMessageRepository
from app.services.chat_service import ChatService
from app.services.conversation_memory_service import ConversationMemoryService
from app.services.mistral_api_service import MistralApiService
from app.services.translation_service import TranslationService
from tests.conftest import FakeLLM


class FakeRag:
    def __init__(self) -> None:
        self.ingested: list[tuple[bytes, str | None, str]] = []
        self.context = ""
        self.deleted_sessions: list[str] = []

    def retrieve_context(self, user_query: str, session_id: str) -> str:
        return self.context

    def ingest_document(self, content: bytes, filename: str | None, session_id: str) -> None:
        self.ingested.append((content, filename, session_id))

    def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)


def build_service(db_session, llm: FakeLLM, rag: FakeRag) -> ChatService:
    mistral = MistralApiService(llm)
    return ChatService(
        chat_repo=ChatMessageRepository(db_session),
        mistral_service=mistral,
        memory_service=ConversationMemoryService(max_history=10),
        translation_service=TranslationService(mistral),
        rag_service=rag,  # type: ignore[arg-type]
    )


def test_system_prompt_is_preserved_verbatim(db_session) -> None:
    llm = FakeLLM()
    rag = FakeRag()
    service = build_service(db_session, llm, rag)
    service.process_user_message("s1", "hello")

    prompt = llm.chat_requests[0]
    assert prompt.startswith(
        "System: You are an advanced AI assistant powered by a Large Language Model architecture. "
        "You are helpful, professional, and capable of general reasoning as well as document-specific analysis.\n\n"
    )
    assert "INSTRUCTIONS:\n" in prompt
    assert "- Do not explicitly mention the presence or absence of documents unless it is directly relevant to the user's query.\n\n" in prompt
    assert prompt.endswith("User: hello")


def test_documents_context_block_appears_when_context_present(db_session) -> None:
    llm = FakeLLM()
    rag = FakeRag()
    rag.context = "retrieved snippet"
    service = build_service(db_session, llm, rag)
    service.process_user_message("s1", "question")

    prompt = llm.chat_requests[0]
    assert (
        "DOCUMENTS CONTEXT:\n"
        "The following information has been retrieved from the user's uploaded documents. "
        "Prioritize this information for accuracy if the user's question relates to it:\n"
        "retrieved snippet\n\n" in prompt
    )


def test_documents_context_block_omitted_when_blank(db_session) -> None:
    llm = FakeLLM()
    rag = FakeRag()
    rag.context = "   "
    service = build_service(db_session, llm, rag)
    service.process_user_message("s1", "question")

    prompt = llm.chat_requests[0]
    assert "DOCUMENTS CONTEXT:" not in prompt
    assert prompt.startswith("System:")


def test_conversation_history_is_injected(db_session) -> None:
    llm = FakeLLM()
    rag = FakeRag()
    service = build_service(db_session, llm, rag)

    service.process_user_message("s1", "first")
    service.process_user_message("s1", "second")

    prompt = llm.chat_requests[1]
    assert "User: first\nAssistant: Test assistant response\nUser: second" in prompt


def test_user_message_is_persisted_without_timestamp(db_session) -> None:
    llm = FakeLLM()
    service = build_service(db_session, llm, FakeRag())
    service.process_user_message("s1", "hello")

    users = db_session.query(ChatMessage).filter_by(role="user").all()
    assert len(users) == 1
    assert users[0].session_id == "s1"
    assert users[0].content == "hello"
    assert users[0].timestamp is None
    assert users[0].detected_language is None
    assert users[0].is_translated is None


def test_assistant_message_has_language_flags_and_timestamp(db_session) -> None:
    llm = FakeLLM()
    llm.chat_response = "Replied"
    service = build_service(db_session, llm, FakeRag())

    reply = service.process_user_message("s1", "hello")
    assert reply.role == "assistant"
    assert reply.content == "Replied"
    assert reply.detected_language == "en"
    assert reply.is_translated is False
    assert reply.timestamp is not None


def test_memory_gets_exchange(db_session) -> None:
    llm = FakeLLM()
    llm.chat_response = "Replied"
    mistral = MistralApiService(llm)
    memory = ConversationMemoryService(max_history=10)
    service = ChatService(
        chat_repo=ChatMessageRepository(db_session),
        mistral_service=mistral,
        memory_service=memory,
        translation_service=TranslationService(mistral),
        rag_service=FakeRag(),  # type: ignore[arg-type]
    )
    service.process_user_message("s1", "hello")
    context = memory.get_context("s1")
    assert context == [{"user": "hello", "assistant": "Replied", "timestamp": context[0]["timestamp"]}]


def test_process_with_file_ingests_then_chats(db_session) -> None:
    llm = FakeLLM()
    rag = FakeRag()
    service = build_service(db_session, llm, rag)

    service.process_user_message_with_file("s1", "what is this?", b"%PDF", "doc.pdf")
    assert rag.ingested == [(b"%PDF", "doc.pdf", "s1")]
    assert len(llm.chat_requests) == 1


def test_ingest_failure_does_not_block_chat(db_session) -> None:
    llm = FakeLLM()

    class ExplodingRag(FakeRag):
        def ingest_document(self, content: bytes, filename: str | None, session_id: str) -> None:
            raise RuntimeError("boom")

    service = build_service(db_session, llm, ExplodingRag())
    reply = service.process_user_message_with_file("s1", "hello", b"%PDF", "doc.pdf")
    assert reply.role == "assistant"


def test_history_and_session_queries(db_session) -> None:
    llm = FakeLLM()
    service = build_service(db_session, llm, FakeRag())
    service.process_user_message("s1", "hi")
    service.process_user_message("s1", "again")
    service.process_user_message("s2", "other")

    history = service.get_chat_history("s1")
    assert [m.content for m in history] == [
        "hi",
        "Test assistant response",
        "again",
"Test assistant response",
    ]

    sessions = service.get_recent_chat_sessions()
    assert {s.session_id for s in sessions} == {"s1", "s2"}

    service.delete_chat_session("s1")
    assert service.get_chat_history("s1") == []
    assert service._rag_service.deleted_sessions == ["s1"]
