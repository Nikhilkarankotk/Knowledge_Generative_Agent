"""Equivalent of ``RagService.java``.

Implements the complete RAG pipeline:

* ``ingest_document`` - parse, chunk (500 chars), embed and persist. Chunks are parsed
  and embedded **before** any existing chunks are deleted, so a failed re-ingest can
  never destroy the session's previously indexed knowledge. Replacement of an existing
  document is atomic (the file's old chunks are deleted and the new ones inserted
  within a single savepoint).
* ``retrieve_context`` - cosine-similarity search over the session's in-memory/DB chunks,
  sorted descending, top-5, joined with newlines.
* ``query`` - builds the exact Java prompt and asks the LLM.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime

from app.core.config import Settings
from app.core.exceptions import IllegalArgumentException, IllegalStateException
from app.models import Document, DocumentChunk
from app.rag.document_parser import DocumentParser
from app.rag.embedding_service import EmbeddingService
from app.rag.text_chunker import TextChunker
from app.repositories import DocumentChunkRepository, DocumentFileRepository, DocumentRepository
from app.services.mistral_api_service import MistralApiService

logger = logging.getLogger(__name__)


class RagService:
    def __init__(
        self,
        document_parser: DocumentParser,
        text_chunker: TextChunker,
        embedding_service: EmbeddingService,
        chunk_repo: DocumentChunkRepository,
        mistral_api_service: MistralApiService,
        chunk_size: int = 500,
        top_k: int = 5,
        document_repo: DocumentRepository | None = None,
        document_file_repo: DocumentFileRepository | None = None,
        answer_service: object | None = None,
    ) -> None:
        self._document_parser = document_parser
        self._text_chunker = text_chunker
        self._embedding_service = embedding_service
        self._chunk_repo = chunk_repo
        self._mistral_api_service = mistral_api_service
        self._chunk_size = chunk_size
        self._top_k = top_k
        self._document_repo = document_repo
        # Optional: persist the uploaded original bytes so the source-aware export
        # can hand back the true native file (NATIVE_FILE) for uploaded documents.
        self._document_file_repo = document_file_repo
        # Optional: a *separate* LLM service used only for final RAG answer
        # generation (e.g. an Azure OpenAI "luna" deployment). Embeddings and OCR
        # always keep using ``mistral_api_service``. Falls back to Mistral when
        # unset so behaviour is unchanged unless a provider is configured.
        self._answer_service = answer_service or mistral_api_service

    @classmethod
    def build(
        cls,
        document_parser: DocumentParser,
        text_chunker: TextChunker,
        embedding_service: EmbeddingService,
        chunk_repo: DocumentChunkRepository,
        mistral_api_service: MistralApiService,
        settings: Settings,
        document_repo: DocumentRepository | None = None,
        document_file_repo: DocumentFileRepository | None = None,
        answer_service: object | None = None,
    ) -> RagService:
        return cls(
            document_parser=document_parser,
            text_chunker=text_chunker,
            embedding_service=embedding_service,
            chunk_repo=chunk_repo,
            mistral_api_service=mistral_api_service,
            chunk_size=settings.rag_chunk_size,
            top_k=settings.rag_top_k,
            document_repo=document_repo,
            document_file_repo=document_file_repo,
            answer_service=answer_service,
        )

    def ingest_document(self, content: bytes, filename: str | None, session_id: str) -> None:
        if not filename:
            raise IllegalArgumentException(
                "A source filename is required to ingest a document."
            )
        # 1. Parse and 2. chunk and 3. embed BEFORE deleting anything. If parsing,
        # chunking or embedding fails, nothing in the database has been touched, so a
        # failed re-ingest can never destroy the previously indexed chunks.
        if self._document_parser.is_image(content, filename):
            text = self._mistral_api_service.ocr_document(filename, content)
        else:
            text = self._document_parser.parse_document(content, filename)
        chunks = self._text_chunker.chunk_text(text, self._chunk_size)

        prepared: list[DocumentChunk] = []
        for chunk in chunks:
            embedding = self._embedding_service.generate_embedding(chunk)
            if embedding is None or len(embedding) == 0:
                raise IllegalStateException(
                    f"Failed to generate embedding for chunk: {chunk}"
                )
            prepared.append(
                DocumentChunk(
                    text=chunk,
                    session_id=session_id,
                    source_filename=filename,
                    embedding=embedding,
                )
            )
            logger.debug("Chunk: %s", chunk)
            logger.debug("Embedding: %s", embedding)

        # 4. Only now replace this file's chunks. The delete + inserts + metadata update
        # run inside a SAVEPOINT so a storage failure rolls back to the previous state.
        try:
            with self._chunk_repo.begin_nested():
                self._chunk_repo.delete_by_session_and_source_filename(session_id, filename)
                for doc_chunk in prepared:
                    self._chunk_repo.save(doc_chunk)
                self._register_document(content, filename, session_id)
        except Exception:
            logger.exception(
                "Failed to store chunks for '%s' in session '%s'", filename, session_id
            )
            raise

    def _register_document(
        self, content: bytes, filename: str | None, session_id: str
    ) -> None:
        if self._document_repo is None or not filename:
            return
        self._document_repo.upsert(
            session_id=session_id,
            filename=filename,
            size_bytes=len(content),
            content_type=self._extension_label(filename),
            status="indexed",
            uploaded_at=datetime.now(),
        )
        if self._document_file_repo is not None:
            self._document_file_repo.upsert(
                session_id=session_id,
                filename=filename,
                content=content,
                content_type=self._extension_label(filename),
            )

    @staticmethod
    def _extension_label(filename: str) -> str:
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        return extension if extension else "unknown"

    def list_documents(self, session_id: str) -> list[Document]:
        if self._document_repo is None:
            return []
        return self._document_repo.find_by_session_id(session_id)

    def delete_session(self, session_id: str) -> None:
        self._chunk_repo.delete_by_session_id(session_id)
        if self._document_repo is not None:
            self._document_repo.delete_by_session_id(session_id)
        if self._document_file_repo is not None:
            self._document_file_repo.delete_by_session_id(session_id)

    def delete_document(self, document_id: int, session_id: str) -> bool:
        if self._document_repo is None:
            return False
        document = self._document_repo.find_by_id(document_id)
        if document is None or document.session_id != session_id:
            return False
        if document.filename:
            self._chunk_repo.delete_by_session_and_source_filename(
                session_id, document.filename
            )
            if self._document_file_repo is not None:
                self._document_file_repo.delete_by_session_and_filename(
                    session_id, document.filename
                )
        else:
            # Pre-migration document row without a filename: clear only its (legacy)
            # NULL-source chunks, never the whole session's knowledge base.
            self._chunk_repo.delete_by_session_and_null_source_filename(session_id)
        self._document_repo.delete_by_id(document.id)
        return True

    def is_knowledge_base_empty(self, session_id: str) -> bool:
        return self._chunk_repo.count_by_session_id(session_id) == 0

    @staticmethod
    def cosine_similarity(vector_a: list[float], vector_b: list[float]) -> float:
        dot_product = 0.0
        norm_a = 0.0
        norm_b = 0.0
        limit = min(len(vector_a), len(vector_b))
        for i in range(limit):
            dot_product += vector_a[i] * vector_b[i]
            norm_a += vector_a[i] ** 2
            norm_b += vector_b[i] ** 2
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot_product / (math.sqrt(norm_a) * math.sqrt(norm_b))

    def retrieve_context(self, user_query: str, session_id: str) -> str:
        if self.is_knowledge_base_empty(session_id):
            return ""
        query_embedding = self._embedding_service.generate_embedding(user_query)
        if query_embedding is None:
            return ""

        all_chunks = self._chunk_repo.find_by_session_id(session_id)
        scored: list[tuple[float, DocumentChunk]] = []
        for chunk in all_chunks:
            if chunk.embedding is not None and len(chunk.embedding) > 0:
                scored.append(
                    (self.cosine_similarity(query_embedding, chunk.embedding), chunk)
                )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        relevant_chunks = [chunk for _, chunk in scored[: self._top_k]]

        return "\n".join(
            self._format_chunk_with_source(chunk) for chunk in relevant_chunks
        )

    @staticmethod
    def _format_chunk_with_source(chunk: DocumentChunk) -> str:
        """Label each retrieved chunk with its real source filename so the LLM can
        attribute information accurately instead of fabricating references."""
        text = chunk.text or ""
        if chunk.source_filename:
            return f"[Source: {chunk.source_filename}]\n{text}"
        return text

    def query(self, user_query: str, session_id: str) -> str:
        context = self.retrieve_context(user_query, session_id)
        augmented_prompt = (
            "You are a portfolio assistant. Use the following context to answer the user's question.\n"
            "If you don't know, say you don't know.\n"
            f"Context: {context}\n"
            f"Question: {user_query}\n"
        )
        return self._answer_service.generate_response(augmented_prompt)
