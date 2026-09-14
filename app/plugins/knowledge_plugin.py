"""KnowledgePlugin - exposes the session's RAG knowledge base to the agent.

Wraps the existing ``RagService`` without reimplementing any RAG behaviour: the
plugin only decides *which* capability to invoke (Semantic Kernel decides), while
``RagService`` owns retrieval details (top-k, similarity, dynamic retrieval rules).
The plugin is constructed per request and bound to the session so concurrent
sessions can never read each other's documents.
"""

from __future__ import annotations

import logging

from semantic_kernel.functions import kernel_function

from app.rag.rag_service import RagService

# The @kernel_function decorator below runs signature introspection at class-definition
# time, which crashes on Python 3.14 unless the compatibility patch is applied first.
from app.sk.compat import apply_py314_compatibility_patch

apply_py314_compatibility_patch()

logger = logging.getLogger(__name__)

SEARCH_KNOWLEDGE_DESCRIPTION = (
    "Search the user's own uploaded documents for the current chat session (PDFs, "
    "DOCX, PPTX, XLSX, TXT and other attached files) using the session's RAG "
    "knowledge base. Use this whenever the question refers to uploaded documents or "
    "attached files. This is different from ConfluencePlugin, which searches "
    "organizational and project documentation in Confluence; if the question could "
    "be answered from both, search both."
)


class KnowledgePlugin:
    """Search the session's uploaded documents via the existing RAG pipeline."""

    def __init__(self, rag_service: RagService, session_id: str) -> None:
        if rag_service is None:
            raise ValueError("rag_service is required for the KnowledgePlugin")
        self._rag_service = rag_service
        self._session_id = session_id

    @kernel_function(description=SEARCH_KNOWLEDGE_DESCRIPTION, name="search_knowledge")
    def search_knowledge(self, query: str) -> str:
        """Return the retrieved document context for ``query`` with source attribution."""
        logger.info(
            "KnowledgePlugin search_knowledge invoked: query=%r session=%r",
            query,
            self._session_id,
        )
        if self._rag_service.is_knowledge_base_empty(self._session_id):
            logger.info("KnowledgePlugin search_knowledge completed: knowledge base empty")
            return (
                "No documents have been uploaded in this session, so there is no document "
                "context available."
            )
        try:
            context = (self._rag_service.retrieve_context(query, self._session_id) or "").strip()
        except Exception:  # noqa: BLE001 - surface a readable marker, never crash the agent
            logger.exception("KnowledgePlugin retrieval failed for session %s", self._session_id)
            return "The document knowledge base could not be searched right now."
        if not context:
            logger.info("KnowledgePlugin search_knowledge completed: no relevant documents")
            return "No relevant documents found in the uploaded knowledge base for this query."
        logger.info(
            "KnowledgePlugin search_knowledge completed: %d sources returned",
            context.count("[Source"),
        )
        return context
