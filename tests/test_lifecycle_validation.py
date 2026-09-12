"""Production-readiness validation of the complete RAG lifecycle.

Covers the behavioural guarantees that matter in production: cross-session isolation of
retrieved chunks, multi-document retrieval with source attribution, document-session
deletion semantics, duplicate/re-ingestion behaviour, persistence across restarts,
every supported ingest format via the HTTP API, and the database-level ``NOT NULL``
constraint on ``document_chunk.source_filename``.
"""

import pytest

from app.core.exceptions import UnsupportedFileTypeError
from app.models import DocumentChunk
from app.rag.rag_service import RagService
from app.rag.text_chunker import TextChunker
from app.repositories import DocumentChunkRepository, DocumentRepository
from app.services.mistral_api_service import MistralApiService
from tests.conftest import FakeLLM, make_pdf_with_text, make_pptx_with_text, make_xlsx_with_text


class StaticEmbeddingService:
    """Returns a fixed query/chunk vector so similarity never affects presence."""

    def __init__(self, query_vector: list[float] | None = None) -> None:
        self.query_vector = query_vector or [1.0, 0.0]

    def generate_embedding(self, text: str) -> list[float]:
        return self.query_vector


class FakeParser:
    def __init__(self, text: str | None = None) -> None:
        self._text = text

    def parse_document(self, content: bytes, filename: str | None) -> str:
        if self._text is not None:
            return self._text
        return content.decode("utf-8", errors="replace")

    def is_image(self, content: bytes, filename: str | None) -> bool:
        return False


def build_service(
    chunk_repo,
    document_repo=None,
    *,
    parser=None,
    llm=None,
    embedding=None,
    chunk_size: int = 500,
    top_k: int = 5,
) -> RagService:
    mistral = MistralApiService(llm or FakeLLM())
    return RagService(
        document_parser=parser or FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=embedding or StaticEmbeddingService(),  # type: ignore[arg-type]
        chunk_repo=chunk_repo,
        mistral_api_service=mistral,
        chunk_size=chunk_size,
        top_k=top_k,
        document_repo=document_repo,
    )


def _chunk_count(session_id: str) -> int:
    from app.api import dependencies

    session = dependencies._database.create_session()
    try:
        return DocumentChunkRepository(session).count_by_session_id(session_id)
    finally:
        session.close()


# ---- Session isolation ----------------------------------------------------------------


def test_retrieve_context_never_leaks_chunks_between_sessions(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    chunk_repo.save(
        DocumentChunk(
            text="secret project insight",
            session_id="session-a",
            source_filename="internal.pdf",
            embedding=[1.0, 0.0],
        )
    )
    service = build_service(chunk_repo)

    assert service.is_knowledge_base_empty("session-b") is True
    assert service.retrieve_context("secret project insight", "session-b") == ""

    context = service.retrieve_context("secret project insight", "session-a")
    assert "[Source: internal.pdf]\nsecret project insight" in context


def test_other_sessions_do_not_see_ingested_documents(client, api_llm) -> None:
    client.post(
        "/api/rag/ingest",
        files={"file": ("private.pdf", make_pdf_with_text("Confidential acquisition strategy"), "application/pdf")},
        headers={"X-Session-ID": "session-a"},
    )
    client.post(
        "/api/chat",
        json={"message": "What is the acquisition?"},
        headers={"X-Session-ID": "session-b"},
    )
    prompt = api_llm.chat_requests[0]
    assert "DOCUMENTS CONTEXT:" not in prompt


def test_chat_uses_ingested_documents_as_context(client, api_llm) -> None:
    client.post(
        "/api/rag/ingest",
        files={"file": ("portfolio.pdf", make_pdf_with_text("Investment committee strategy details"), "application/pdf")},
        headers={"X-Session-ID": "ctx-session"},
    )
    client.post(
        "/api/chat",
        json={"message": "summarize"},
        headers={"X-Session-ID": "ctx-session"},
    )
    prompt = api_llm.chat_requests[0]
    assert "DOCUMENTS CONTEXT:" in prompt
    assert "[Source: portfolio.pdf]" in prompt


# ---- Multi-document retrieval + attribution -------------------------------------------


def test_query_after_multidocument_ingest_grounds_both_sources(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)
    llm = FakeLLM()
    llm.chat_response = "Grounded answer"
    service = build_service(chunk_repo, document_repo, llm=llm)

    service.ingest_document(b"one", "a.pdf", "s-q")
    service.ingest_document(b"two", "b.pdf", "s-q")

    answer = service.query("summarize the files", "s-q")
    assert answer == "Grounded answer"
    prompt = llm.chat_requests[0]
    assert "[Source: a.pdf]" in prompt
    assert "[Source: b.pdf]" in prompt


# ---- Duplicate upload -----------------------------------------------------------------


def test_duplicate_upload_updates_document_and_replaces_chunks(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)
    service = build_service(chunk_repo, document_repo, chunk_size=10)

    first_payload = b"AAAA-BBBB-CCCC-DDDD-EEEE"
    second_payload = b"1111-2222-3333-4444-5555"
    assert len(first_payload) == len(second_payload)

    service.ingest_document(first_payload, "report.pdf", "s-dup")
    first_ids = {c.id for c in chunk_repo.find_by_session_id("s-dup")}
    assert len(first_ids) > 1
    assert document_repo.find_by_session_and_filename("s-dup", "report.pdf") is not None

    service.ingest_document(second_payload, "report.pdf", "s-dup")

    documents = document_repo.find_by_session_id("s-dup")
    assert [d.filename for d in documents] == ["report.pdf"]
    assert documents[0].size_bytes == len(second_payload)

    chunks = chunk_repo.find_by_session_id("s-dup")
    assert len(chunks) == len(first_ids)
    assert all("AAAA" not in (c.text or "") for c in chunks)
    assert any("1111" in (c.text or "") for c in chunks)


# ---- Document deletion keeps the rest searchable --------------------------------------


def test_delete_document_keeps_other_documents_searchable(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)
    service = build_service(chunk_repo, document_repo)

    service._document_parser = FakeParser("KEEP this memorable phrase about retention")
    service.ingest_document(b"keep", "keep.pdf", "s-del")
    service._document_parser = FakeParser("REMOVABLE secret data")
    service.ingest_document(b"remove", "remove.pdf", "s-del")

    target = document_repo.find_by_session_and_filename("s-del", "remove.pdf")
    assert target is not None
    assert service.delete_document(target.id, "s-del") is True

    assert [d.filename for d in service.list_documents("s-del")] == ["keep.pdf"]
    context = service.retrieve_context("retention", "s-del")
    assert "[Source: keep.pdf]" in context
    assert "KEEP this memorable phrase" in context
    assert "REMOVABLE" not in context


def test_api_delete_document_keeps_other_documents_searchable(client, api_llm) -> None:
    client.post(
        "/api/rag/ingest",
        files={"file": ("a.pdf", make_pdf_with_text("Alpha confidential plan details"), "application/pdf")},
        headers={"X-Session-ID": "keep-session"},
    )
    client.post(
        "/api/rag/ingest",
        files={"file": ("b.pdf", make_pdf_with_text("Beta strategic roadmap"), "application/pdf")},
        headers={"X-Session-ID": "keep-session"},
    )
    a_id = [
        d["id"]
        for d in client.get("/api/rag/documents", headers={"X-Session-ID": "keep-session"}).json()
        if d["filename"] == "a.pdf"
    ][0]

    deleted = client.delete(f"/api/rag/documents/{a_id}", headers={"X-Session-ID": "keep-session"})
    assert deleted.status_code == 200

    remaining = client.get("/api/rag/documents", headers={"X-Session-ID": "keep-session"}).json()
    assert [d["filename"] for d in remaining] == ["b.pdf"]

    client.post(
        "/api/chat",
        json={"message": "What is in the documents?"},
        headers={"X-Session-ID": "keep-session"},
    )
    prompt = api_llm.chat_requests[0]
    assert "DOCUMENTS CONTEXT:" in prompt
    assert "[Source: b.pdf]" in prompt
    assert "[Source: a.pdf]" not in prompt


# ---- Session deletion -----------------------------------------------------------------


def test_delete_session_removes_messages_documents_and_chunks(client, api_llm) -> None:
    client.post(
        "/api/rag/ingest",
        files={"file": ("victim.pdf", make_pdf_with_text("Victim content"), "application/pdf")},
        headers={"X-Session-ID": "victim"},
    )
    client.post(
        "/api/rag/ingest",
        files={"file": ("survivor.pdf", make_pdf_with_text("Survivor content"), "application/pdf")},
        headers={"X-Session-ID": "survivor"},
    )
    client.post("/api/chat", json={"message": "hello victim"}, headers={"X-Session-ID": "victim"})
    client.post("/api/chat", json={"message": "hello survivor"}, headers={"X-Session-ID": "survivor"})

    assert _chunk_count("victim") > 0
    assert _chunk_count("survivor") > 0

    deleted = client.delete("/api/history/sessions/victim")
    assert deleted.status_code == 200

    assert client.get("/api/history", headers={"X-Session-ID": "victim"}).json() == []
    assert client.get("/api/rag/documents", headers={"X-Session-ID": "victim"}).json() == []
    assert _chunk_count("victim") == 0

    survivor_history = client.get("/api/history", headers={"X-Session-ID": "survivor"}).json()
    assert [e["role"] for e in survivor_history] == ["user", "assistant"]
    survivor_docs = client.get("/api/rag/documents", headers={"X-Session-ID": "survivor"}).json()
    assert [d["filename"] for d in survivor_docs] == ["survivor.pdf"]
    assert _chunk_count("survivor") > 0


# ---- Persistence across refresh ------------------------------------------------


def test_knowledge_base_and_metadata_persist_across_refresh(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)
    build_service(chunk_repo, document_repo).ingest_document(b"payload", "notes.pdf", "s-persist")
    db_session.commit()

    from app.api import dependencies

    fresh = dependencies._database.create_session()
    try:
        restored = build_service(DocumentChunkRepository(fresh), DocumentRepository(fresh))
        assert restored.is_knowledge_base_empty("s-persist") is False
        assert [d.filename for d in restored.list_documents("s-persist")] == ["notes.pdf"]
        context = restored.retrieve_context("what", "s-persist")
        assert "[Source: notes.pdf]" in context
    finally:
        fresh.close()


# ---- Every supported format via the HTTP API -------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content", "content_type"),
    [
        (
            "data.xlsx",
            make_xlsx_with_text("Quarterly figures cell"),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "deck.pptx",
            make_pptx_with_text("Roadmap slide"),
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
        ("team.csv", b"name,role\nalice,engineer\nbob,pm", "text/csv"),
        ("profile.json", b'{"name":"Karan","years":"5"}', "application/json"),
        ("page.html", b"<html><body><p>Hello <b>World</b></p></body></html>", "text/html"),
    ],
)
def test_rag_ingest_supports_each_remaining_format(
    client, api_llm, filename: str, content: bytes, content_type: str
) -> None:
    response = client.post(
        "/api/rag/ingest",
        files={"file": (filename, content, content_type)},
        headers={"X-Session-ID": "fmt-session"},
    )
    assert response.status_code == 200


def test_rag_documents_list_includes_all_ingested_formats(client, api_llm) -> None:
    payloads = [
        ("data.xlsx", make_xlsx_with_text("cell"), "application/octet-stream"),
        ("deck.pptx", make_pptx_with_text("slide"), "application/octet-stream"),
        ("team.csv", b"name,role\nalice,engineer", "text/csv"),
        ("profile.json", b'{"name":"Karan"}', "application/json"),
        ("page.html", b"<html><body><p>Hello</p></body></html>", "text/html"),
    ]
    for filename, content, content_type in payloads:
        response = client.post(
            "/api/rag/ingest",
            files={"file": (filename, content, content_type)},
            headers={"X-Session-ID": "fmt-list"},
        )
        assert response.status_code == 200

    documents = client.get("/api/rag/documents", headers={"X-Session-ID": "fmt-list"}).json()
    assert {d["filename"] for d in documents} == {filename for filename, _, _ in payloads}
    assert all(d["status"] == "indexed" for d in documents)


# ---- Failure-safe re-ingest through the API -------------------------------------------


def test_api_failed_reingest_preserves_knowledge(client, api_llm, monkeypatch) -> None:
    ingest = client.post(
        "/api/rag/ingest",
        files={"file": ("portfolio.pdf", make_pdf_with_text("Investment committee strategy details"), "application/pdf")},
        headers={"X-Session-ID": "res-fail"},
    )
    assert ingest.status_code == 200
    chunk_count_before = _chunk_count("res-fail")
    assert chunk_count_before > 0

    class FailingParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            raise UnsupportedFileTypeError("broken parser")

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    from app.api import dependencies

    monkeypatch.setattr(dependencies, "_document_parser", FailingParser())

    failed = client.post(
        "/api/rag/ingest",
        files={"file": ("portfolio.pdf", make_pdf_with_text("Broken replacement"), "application/pdf")},
        headers={"X-Session-ID": "res-fail"},
    )
    assert failed.status_code == 400

    documents = client.get("/api/rag/documents", headers={"X-Session-ID": "res-fail"}).json()
    assert [d["filename"] for d in documents] == ["portfolio.pdf"]
    assert _chunk_count("res-fail") == chunk_count_before

    client.post("/api/chat", json={"message": "summarize"}, headers={"X-Session-ID": "res-fail"})
    prompt = api_llm.chat_requests[0]
    assert "[Source: portfolio.pdf]" in prompt


# ---- DB constraint: source_filename is required ---------------------------------------


def test_chunk_without_source_filename_rejected_by_database(db_session) -> None:
    from sqlalchemy.exc import IntegrityError

    repo = DocumentChunkRepository(db_session)
    with pytest.raises(IntegrityError, match="source_filename"):
        repo.save(
            DocumentChunk(
                text="orphan", session_id="s", source_filename=None, embedding=[1.0]
            )
        )
    db_session.rollback()
