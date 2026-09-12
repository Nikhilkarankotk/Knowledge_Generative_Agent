"""Tests for the repository layer (JPA repository equivalents)."""

from datetime import datetime

from app.models import ChatMessage, Document, DocumentChunk
from app.repositories import (
    ChatMessageRepository,
    DocumentChunkRepository,
    DocumentRepository,
    FeedbackRepository,
)


def test_find_recent_sessions_returns_first_user_message_per_session(db_session) -> None:
    repo = ChatMessageRepository(db_session)
    # session A: user messages at ids 1,2 (first user message is id 1)
    repo.save(ChatMessage(session_id="A", role="user", content="a1"))
    repo.save(ChatMessage(session_id="A", role="user", content="a2"))
    # session B: first user message is id 3, with a later assistant message
    repo.save(ChatMessage(session_id="B", role="user", content="b1", timestamp=datetime(2024, 1, 1)))
    repo.save(ChatMessage(session_id="B", role="assistant", content="b2", timestamp=datetime(2024, 1, 2)))

    recent = repo.find_recent_chat_sessions()
    sessions = {message.session_id: message.content for message in recent}
    assert sessions == {"A": "a1", "B": "b1"}
    # Only the MIN user id per session is returned (not later messages).
    assert {message.role for message in recent} == {"user"}


def test_find_by_session_id_orders_by_id(db_session) -> None:
    repo = ChatMessageRepository(db_session)
    repo.save(ChatMessage(session_id="s", role="user", content="first"))
    repo.save(ChatMessage(session_id="s", role="assistant", content="second"))
    repo.save(ChatMessage(session_id="other", role="user", content="other"))
    history = repo.find_by_session_id("s")
    assert [m.content for m in history] == ["first", "second"]


def test_delete_by_session_id_removes_only_that_session(db_session) -> None:
    repo = ChatMessageRepository(db_session)
    repo.save(ChatMessage(session_id="s1", role="user", content="a"))
    repo.save(ChatMessage(session_id="s2", role="user", content="b"))
    repo.delete_by_session_id("s1")
    assert [m.session_id for m in repo.find_by_session_id("s1")] == []
    assert len(repo.find_by_session_id("s2")) == 1


def test_document_chunk_repository_crud(db_session) -> None:
    repo = DocumentChunkRepository(db_session)
    chunk = repo.save(DocumentChunk(text="txt", session_id="s", source_filename="a.pdf", embedding=[0.1, 0.2]))
    assert chunk.id is not None
    assert repo.count_by_session_id("s") == 1
    assert [c.text for c in repo.find_by_session_id("s")] == ["txt"]
    repo.delete_by_session_id("s")
    assert repo.count_by_session_id("s") == 0


def test_document_chunk_delete_by_session_and_source_filename_only_removes_that_file(db_session) -> None:
    repo = DocumentChunkRepository(db_session)
    repo.save(DocumentChunk(text="a", session_id="s", source_filename="a.pdf"))
    repo.save(DocumentChunk(text="b", session_id="s", source_filename="b.pdf"))
    repo.delete_by_session_and_source_filename("s", "a.pdf")
    # a.pdf chunks are removed; b.pdf chunks are preserved.
    assert [c.text for c in repo.find_by_session_id("s")] == ["b"]


def test_document_chunk_delete_by_session_and_source_filename_is_strict(db_session) -> None:
    repo = DocumentChunkRepository(db_session)
    repo.save(DocumentChunk(text="a", session_id="s", source_filename="a.pdf"))
    repo.save(DocumentChunk(text="b", session_id="s", source_filename="b.pdf"))
    repo.delete_by_session_and_source_filename("s", "a.pdf")
    assert [c.text for c in repo.find_by_session_id("s")] == ["b"]


def test_document_repository_crud_and_upsert(db_session) -> None:
    repo = DocumentRepository(db_session)
    doc = repo.save(Document(session_id="s", filename="a.pdf", status="indexed"))
    assert doc.id is not None
    assert [d.filename for d in repo.find_by_session_id("s")] == ["a.pdf"]

    upserted = repo.upsert(
        session_id="s",
        filename="a.pdf",
        size_bytes=100,
        content_type="pdf",
        status="indexed",
        uploaded_at=datetime(2024, 1, 1),
    )
    assert upserted.id == doc.id
    assert upserted.size_bytes == 100
    assert len(repo.find_by_session_id("s")) == 1

    other = repo.upsert(
        session_id="s",
        filename="b.pdf",
        size_bytes=200,
        content_type="pdf",
        status="indexed",
        uploaded_at=datetime(2024, 1, 2),
    )
    assert other.id != doc.id
    assert len(repo.find_by_session_id("s")) == 2

    repo.delete_by_id(doc.id)
    assert [d.filename for d in repo.find_by_session_id("s")] == ["b.pdf"]

    repo.delete_by_session_id("s")
    assert repo.find_by_session_id("s") == []


def test_feedback_repository_by_message_id(db_session) -> None:
    message = ChatMessageRepository(db_session).save(
        ChatMessage(session_id="s", role="assistant", content="reply")
    )
    repo = FeedbackRepository(db_session)
    assert repo.find_by_message_id(message.id) is None

    from app.models import Feedback

    repo.save(Feedback(message_id=message.id, rating=1, corrected_answer="ok"))
    fetched = repo.find_by_message_id(message.id)
    assert fetched is not None
    assert fetched.rating == 1
