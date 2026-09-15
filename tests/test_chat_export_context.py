"""ChatService export-context persistence + session cleanup tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.export.capture import ExportItemDraft, RetrievalCapture
from app.export.formats import ScenarioType
from app.models import ChatMessage, DocumentFile, ExportContext
from app.repositories import (
    ChatMessageRepository,
    DocumentFileRepository,
    ExportContextRepository,
)
from app.services.chat_service import ChatService
from app.services.conversation_memory_service import ConversationMemoryService
from app.services.mistral_api_service import MistralApiService
from app.services.translation_service import TranslationService
from tests.conftest import FakeLLM

SETTINGS = SimpleNamespace(export_context_ttl_days=30, export_include_summary=True)


class FakeRag:
    def __init__(self, context: str = "", *, empty: bool = False) -> None:
        self._context = context
        self._empty = empty
        self.ingested: list[tuple[bytes, str | None, str]] = []
        self.deleted_sessions: list[str] = []

    def is_knowledge_base_empty(self, session_id: str) -> bool:
        return self._empty

    def retrieve_context(self, user_query: str, session_id: str) -> str:
        return self._context

    def ingest_document(self, content: bytes, filename: str | None, session_id: str) -> None:
        self.ingested.append((content, filename, session_id))

    def delete_session(self, session_id: str) -> None:
        self.deleted_sessions.append(session_id)


def _build(db_session, rag: FakeRag) -> ChatService:
    mistral = MistralApiService(FakeLLM())
    return ChatService(
        chat_repo=ChatMessageRepository(db_session),
        mistral_service=mistral,
        memory_service=ConversationMemoryService(max_history=10),
        translation_service=TranslationService(mistral),
        rag_service=rag,  # type: ignore[arg-type]
        export_repo=ExportContextRepository(db_session),
        document_file_repo=DocumentFileRepository(db_session),
        settings=SETTINGS,
    )


def test_legacy_path_persists_export_context(db_session) -> None:
    service = _build(db_session, FakeRag("[Source: report.pdf]\nMonthly report summary"))
    assistant = service.process_user_message("s1", "show me the report")

    repo = ExportContextRepository(db_session)
    context = repo.find_by_chat_message_id(assistant.id)

    assert context is not None
    assert context.session_id == "s1"
    assert context.source_count == 1
    assert context.expires_at is not None
    items = repo.find_items(context.id)
    assert len(items) == 1
    item = items[0]
    assert item.source_type == "UPLOADED_DOCUMENT"
    assert item.filename == "report.pdf"
    assert item.content_reference is not None and "Monthly report summary" in item.content_reference


def test_delete_session_removes_export_context_and_document_files(db_session) -> None:
    service = _build(db_session, FakeRag("[Source: policy.pdf]\nPolicy text"))
    assistant = service.process_user_message("s2", "what is the policy?")

    context = ExportContextRepository(db_session).find_by_chat_message_id(assistant.id)
    assert context is not None

    session_id = "s2"
    db_session.add(
        DocumentFile(
            session_id=session_id,
            filename="policy.pdf",
            content=b"raw",
            content_type="application/pdf",
        )
    )
    db_session.commit()

    db_session.query(ExportContext).filter_by(session_id=session_id).update({"status": "ready"})
    db_session.commit()

    service.delete_chat_session(session_id)

    assert ExportContextRepository(db_session).find_by_chat_message_id(assistant.id) is None
    assert DocumentFileRepository(db_session).find_by_session_and_filename(session_id, "policy.pdf") is None
    assert ChatMessageRepository(db_session).find_by_session_id(session_id) == []


def test_captured_metadata_is_persisted(db_session) -> None:
    service = _build(db_session, FakeRag("[Source: a.pdf]\nfirst\n\n[Source: b.pdf]\nsecond"))
    assistant = service.process_user_message("s3", "compare a and b")
    items = ExportContextRepository(db_session).find_items_by_chat_message_id(assistant.id)

    filenames = {item.filename for item in items}
    assert filenames == {"a.pdf", "b.pdf"}
    for item in items:
        assert item.exportable is True
        assert item.retrieval_rank is not None


def test_persisted_capture_sources_share_one_retrieval_context(db_session) -> None:
    """Confluence + SharePoint sources captured in one turn persist as one context."""
    service = _build(db_session, FakeRag())

    chat = ChatMessageRepository(db_session).save(
        ChatMessage(session_id="s4", role="assistant", content="n/a")
    )

    capture = RetrievalCapture()
    capture.add(
        ExportItemDraft(
            source_type="CONFLUENCE",
            source_id="DOC1",
            source_name="API Documentation",
            source_url="https://wiki.example.com/spaces/PAY/pages/DOC1",
            export_strategy=ScenarioType.GENERATED_DOCUMENT.value,
        )
    )
    capture.add(
        ExportItemDraft(
            source_type="SHAREPOINT",
            source_id="01onboard",
            source_name="Onboarding Process.docx",
            source_url="https://knowledgegenagent.sharepoint.com/drives/b!drive/items/01onboard",
            export_strategy=ScenarioType.GENERATED_DOCUMENT.value,
        )
    )

    service._commit_export_context("s4", chat.id, capture)

    repo = ExportContextRepository(db_session)
    context = repo.find_by_chat_message_id(chat.id)
    assert context is not None
    assert context.source_count == 2
    items = repo.find_items_by_chat_message_id(chat.id)
    assert {item.source_type for item in items} == {"CONFLUENCE", "SHAREPOINT"}
    assert {item.source_id for item in items} == {"DOC1", "01onboard"}
    assert all(item.exportable for item in items)
