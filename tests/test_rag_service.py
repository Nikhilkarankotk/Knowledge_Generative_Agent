"""Tests for the RAG retrieval / cosine-similarity logic (RagService.java)."""

import pytest

from app.core.exceptions import IllegalStateException
from app.models import DocumentChunk
from app.rag.rag_service import RagService
from app.rag.text_chunker import TextChunker
from app.repositories import DocumentChunkRepository, DocumentRepository
from app.services.mistral_api_service import MistralApiService
from tests.conftest import FakeLLM, make_pdf_with_text, make_png_bytes


class StaticEmbeddingService:
    """Returns a fixed query embedding supplied by the test."""

    def __init__(self, query_vector: list[float]) -> None:
        self.query_vector = query_vector
        self.calls: list[str] = []

    def generate_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        return self.query_vector


class NoPyEmbeddingService:
    def generate_embedding(self, text: str) -> None:
        return None


@pytest.fixture
def rag_service(db_session) -> RagService:
    return RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0, 0.0]),
        chunk_repo=DocumentChunkRepository(db_session),
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        top_k=5,
    )


def save_chunk(db_session, session_id: str, text: str, embedding: list[float], *, source_filename: str = "doc.txt") -> None:
    repo = DocumentChunkRepository(db_session)
    repo.save(DocumentChunk(text=text, session_id=session_id, source_filename=source_filename, embedding=embedding))


def test_empty_knowledge_base_returns_empty_context(rag_service) -> None:
    assert rag_service.is_knowledge_base_empty("s1") is True
    assert rag_service.retrieve_context("question", "s1") == ""


def test_cosine_similarity_matches_java_math() -> None:
    assert RagService.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert RagService.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert RagService.cosine_similarity([2.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    ortho = RagService.cosine_similarity([1.0, 2.0], [2.0, 1.0])
    assert ortho == pytest.approx(4.0 / 5.0)


def test_cosine_similarity_zero_norm_returns_zero() -> None:
    assert RagService.cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert RagService.cosine_similarity([1.0, 1.0], [0.0, 0.0]) == 0.0


def test_cosine_similarity_stops_at_shortest_vector() -> None:
    # Java loop condition: i < vectorA.length && i < vectorB.size()
    assert RagService.cosine_similarity([1.0, 2.0, 3.0], [1.0]) == pytest.approx(1.0)


def test_retrieve_context_returns_top_k_joined_by_newline(db_session, rag_service) -> None:
    save_chunk(db_session, "s1", "chunk alpha", [1.0, 0.0, 0.0])
    save_chunk(db_session, "s1", "chunk beta", [0.9, 0.1, 0.0])
    save_chunk(db_session, "s1", "chunk gamma", [0.0, 1.0, 0.0])
    context = rag_service.retrieve_context("query", "s1")
    assert (
        "\n".join(
            [
                "[Source: doc.txt]\nchunk alpha",
                "[Source: doc.txt]\nchunk beta",
                "[Source: doc.txt]\nchunk gamma",
            ]
        )
        == context
    )


def test_retrieve_context_limits_to_top_k(db_session) -> None:
    service = RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=DocumentChunkRepository(db_session),
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        top_k=2,
    )
    for i in range(5):
        save_chunk(db_session, "s2", f"chunk {i}", [1.0 - i * 0.1, i * 0.1])
    context = service.retrieve_context("query", "s2")
    lines = [line for line in context.split("\n") if not line.startswith("[Source:")]
    assert len(lines) == 2
    assert lines[0] == "chunk 0"


def test_retrieve_context_ignores_chunks_without_embedding(db_session, rag_service) -> None:
    save_chunk(db_session, "s1", "no embedding", None)
    save_chunk(db_session, "s1", "with embedding", [1.0, 0.0])
    context = rag_service.retrieve_context("query", "s1")
    assert context == "[Source: doc.txt]\nwith embedding"


def test_retrieve_context_labels_chunks_with_source_filename(db_session, rag_service) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    chunk_repo.save(
        DocumentChunk(text="alpha content", session_id="s1", source_filename="a.pdf", embedding=[1.0, 0.0])
    )
    chunk_repo.save(
        DocumentChunk(text="beta content", session_id="s1", source_filename="b.txt", embedding=[0.9, 0.1])
    )
    context = rag_service.retrieve_context("query", "s1")
    assert "[Source: a.pdf]\nalpha content" in context
    assert "[Source: b.txt]\nbeta content" in context


def test_retrieve_context_query_embedding_none_returns_empty(db_session) -> None:
    service = RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=NoPyEmbeddingService(),
        chunk_repo=DocumentChunkRepository(db_session),
        mistral_api_service=MistralApiService(FakeLLM()),
    )
    save_chunk(db_session, "s3", "chunk", [1.0, 0.0])
    assert service.retrieve_context("query", "s3") == ""


def test_ingest_document_clears_previous_chunks_and_embeds(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    chunk_repo.save(DocumentChunk(text="old", session_id="s4", source_filename="x.pdf", embedding=[0.0]))
    db_session.commit()

    embedding_service = StaticEmbeddingService([1.0, 0.0])
    service = RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=embedding_service,
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
chunk_size=10,
    )

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service._document_parser = FakeParser()
    service.ingest_document(make_pdf_with_text("x"), "x.pdf", "s4")

    chunks = chunk_repo.find_by_session_id("s4")
    assert len(chunks) == 3  # 26 chars / 10 = 3 chunks
    assert all(chunk.text and chunk.embedding for chunk in chunks)
    assert all(chunk.text != "old" for chunk in chunks)


def test_embedding_failure_raises_illegal_state(db_session) -> None:
    class EmptyEmbeddingService:
        def generate_embedding(self, text: str) -> list[float]:
            return []

    service = RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=EmptyEmbeddingService(),  # type: ignore[arg-type]
        chunk_repo=DocumentChunkRepository(db_session),
        mistral_api_service=MistralApiService(FakeLLM()),
chunk_size=500,
    )

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "some text"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service._document_parser = FakeParser()
    with pytest.raises(IllegalStateException, match="Failed to generate embedding"):
        service.ingest_document(b"data", "x.pdf", "s5")


def test_query_builds_prompt_and_returns_llm_response(db_session) -> None:
    llm = FakeLLM()
    llm.chat_response = "Portfolio answer"
    save_chunk(db_session, "s6", "project insight", [1.0, 0.0])

    service = RagService(
        document_parser=None,  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=DocumentChunkRepository(db_session),
        mistral_api_service=MistralApiService(llm),
        chunk_size=500,
top_k=5,
    )

    answer = service.query("Tell me about the project", "s6")
    assert answer == "Portfolio answer"
    prompt = llm.chat_requests[0]
    assert "You are a portfolio assistant." in prompt
    assert "Context: [Source: doc.txt]\nproject insight" in prompt
    assert "Question: Tell me about the project" in prompt


def test_ingest_image_routes_text_through_ocr(db_session) -> None:
    seen: dict[str, str] = {}

    class ImageFakeParser:
        def is_image(self, content: bytes, filename: str | None) -> bool:
            return True

    class SpyApi:
        def ocr_document(self, filename: str | None, content: bytes) -> str:
            seen["filename"] = filename or ""
            return "OCR extracted text"

    chunk_repo = DocumentChunkRepository(db_session)
    service = RagService(
        document_parser=ImageFakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=SpyApi(),  # type: ignore[arg-type]
        chunk_size=500,
    )
    service.ingest_document(make_png_bytes(), "scan.png", "s7")

    assert seen["filename"] == "scan.png"
    chunks = chunk_repo.find_by_session_id("s7")
    assert len(chunks) == 1
    assert chunks[0].text == "OCR extracted text"


def test_ingest_multiple_documents_keeps_each_files_chunks(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "alpha beta gamma"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    def build_service() -> RagService:
        return RagService(
            document_parser=FakeParser(),  # type: ignore[arg-type]
            text_chunker=TextChunker(),
            embedding_service=StaticEmbeddingService([1.0, 0.0]),
            chunk_repo=chunk_repo,
            mistral_api_service=MistralApiService(FakeLLM()),
            chunk_size=10,
            document_repo=document_repo,
        )

    service = build_service()
    service.ingest_document(b"payload-one", "one.pdf", "s-multi")
    service.ingest_document(b"payload-two", "two.pdf", "s-multi")

    # Chunks from both documents coexist (multi-doc knowledge base).
    sources = {c.source_filename for c in chunk_repo.find_by_session_id("s-multi")}
    assert sources == {"one.pdf", "two.pdf"}

    # Document metadata was registered for both.
    filenames = [d.filename for d in document_repo.find_by_session_id("s-multi")]
    assert filenames == ["one.pdf", "two.pdf"]
    sizes = {d.filename: d.size_bytes for d in document_repo.find_by_session_id("s-multi")}
    assert sizes["one.pdf"] == len(b"payload-one")
    assert sizes["two.pdf"] == len(b"payload-two")


def test_reingesting_same_file_replaces_only_that_files_chunks(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "".join("content " for _ in range(40))

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=10,
        document_repo=document_repo,
    )
    service.ingest_document(b"first", "report.pdf", "s-re")
    service.ingest_document(b"second", "notes.pdf", "s-re")
    first_ids = {c.id for c in chunk_repo.find_by_session_id("s-re")}

    service.ingest_document(b"third", "report.pdf", "s-re")

    remaining = chunk_repo.find_by_session_id("s-re")
    sources = {c.source_filename for c in remaining}
    assert sources == {"report.pdf", "notes.pdf"}
    assert all(c.id not in first_ids for c in remaining if c.source_filename == "report.pdf")
    assert any(c.source_filename == "notes.pdf" for c in remaining)
    assert document_repo.find_by_session_and_filename("s-re", "report.pdf") is not None
    assert document_repo.find_by_session_and_filename("s-re", "notes.pdf") is not None


def test_delete_document_removes_metadata_and_its_chunks(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "long enough text to chunk more than once"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        document_repo=document_repo,
    )
    service.ingest_document(b"keep", "keep.pdf", "s-del")
    service.ingest_document(b"remove", "remove.pdf", "s-del")
    target_id = document_repo.find_by_session_and_filename("s-del", "remove.pdf").id

    assert service.delete_document(target_id, "s-del") is True
    assert document_repo.find_by_id(target_id) is None
    assert [d.filename for d in document_repo.find_by_session_id("s-del")] == ["keep.pdf"]
    remaining_sources = {c.source_filename for c in chunk_repo.find_by_session_id("s-del")}
    assert remaining_sources == {"keep.pdf"}


def test_delete_document_scoped_to_session(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "content for chunking purposes here"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        document_repo=document_repo,
    )
    service.ingest_document(b"a", "x.pdf", "s-one")
    doc_id = document_repo.find_by_session_and_filename("s-one", "x.pdf").id

    assert service.delete_document(doc_id, "s-other") is False
    assert document_repo.find_by_id(doc_id) is not None


def test_delete_session_removes_chunks_and_documents(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "content for chunking purposes here"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        document_repo=document_repo,
    )
    service.ingest_document(b"a", "a.pdf", "s-clr")
    service.ingest_document(b"b", "b.pdf", "s-clr")

    service.delete_session("s-clr")
    assert chunk_repo.count_by_session_id("s-clr") == 0
    assert document_repo.find_by_session_id("s-clr") == []


def test_ingest_requires_source_filename(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "text"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
    )
    with pytest.raises(Exception, match="source filename is required"):
        service.ingest_document(b"data", None, "s-req")
    assert chunk_repo.count_by_session_id("s-req") == 0


def test_failed_reingest_preserves_existing_chunks(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "long enough text to chunk into multiple pieces"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        document_repo=document_repo,
    )
    service.ingest_document(b"first", "report.pdf", "s-safe")

    # Re-ingest with a failing embedding service; nothing should be replaced.
    class EmptyEmbeddingService:
        def generate_embedding(self, text: str) -> list[float]:
            return []

    failing = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=EmptyEmbeddingService(),  # type: ignore[arg-type]
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        document_repo=document_repo,
    )
    with pytest.raises(IllegalStateException, match="Failed to generate embedding"):
        failing.ingest_document(b"second", "report.pdf", "s-safe")

    remaining = chunk_repo.find_by_session_id("s-safe")
    assert len(remaining) == 1
    assert remaining[0].source_filename == "report.pdf"
    assert remaining[0].text != "second"
    assert document_repo.find_by_session_and_filename("s-safe", "report.pdf") is not None


def test_parse_failure_during_reingest_keeps_existing_chunks(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "long enough text to chunk into multiple pieces"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    service = RagService(
        document_parser=FakeParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        document_repo=document_repo,
    )
    service.ingest_document(b"first", "report.pdf", "s-parse")

    class BrokenParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            raise RuntimeError("broken parser")

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    broken = RagService(
        document_parser=BrokenParser(),  # type: ignore[arg-type]
        text_chunker=TextChunker(),
        embedding_service=StaticEmbeddingService([1.0, 0.0]),
        chunk_repo=chunk_repo,
        mistral_api_service=MistralApiService(FakeLLM()),
        chunk_size=500,
        document_repo=document_repo,
    )
    with pytest.raises(RuntimeError, match="broken parser"):
        broken.ingest_document(b"second", "report.pdf", "s-parse")

    remaining = chunk_repo.find_by_session_id("s-parse")
    assert len(remaining) == 1
    assert remaining[0].source_filename == "report.pdf"


def test_reingest_atomic_replace_keeps_other_files(db_session) -> None:
    chunk_repo = DocumentChunkRepository(db_session)
    document_repo = DocumentRepository(db_session)

    class FakeParser:
        def parse_document(self, content: bytes, filename: str | None) -> str:
            return "alpha beta gamma delta"

        def is_image(self, content: bytes, filename: str | None) -> bool:
            return False

    def build_service() -> RagService:
        return RagService(
            document_parser=FakeParser(),  # type: ignore[arg-type]
            text_chunker=TextChunker(),
            embedding_service=StaticEmbeddingService([1.0, 0.0]),
            chunk_repo=chunk_repo,
            mistral_api_service=MistralApiService(FakeLLM()),
            chunk_size=10,
            document_repo=document_repo,
        )

    service = build_service()
    service.ingest_document(b"a", "a.pdf", "s-atomic")
    service.ingest_document(b"b", "b.pdf", "s-atomic")
    before: dict[str, set[str]] = {}
    for c in chunk_repo.find_by_session_id("s-atomic"):
        if c.text is not None:
            before.setdefault(c.source_filename, set()).add(c.text)

    service.ingest_document(b"c", "c.pdf", "s-atomic")

    after = chunk_repo.find_by_session_id("s-atomic")
    sources = {c.source_filename for c in after}
    assert sources == {"a.pdf", "b.pdf", "c.pdf"}
    for c in after:
        if c.source_filename != "c.pdf":
            assert c.text in before[c.source_filename]
    assert document_repo.find_by_session_and_filename("s-atomic", "c.pdf") is not None
