"""Equivalent of ``ChatService.java``.

Two paths:

* **Legacy path** (Semantic Kernel disabled) preserves the exact augmented-prompt
  construction used by the Java application: system preamble, optional DOCUMENTS
  CONTEXT block, INSTRUCTIONS block, conversation history exchanges, and the final
  user message - in that order.
* **Knowledge Generative Agent path** (``semantic_kernel_factory`` provided) routes
  the turn through the Semantic Kernel agent (KnowledgePlugin + ConfluencePlugin)
  with per-session isolation. PostgreSQL ``chat_message`` remains the source of
  truth; Semantic Kernel's chat history is a runtime view. If the agent fails, the
  service transparently falls back to the legacy prompt path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from app.export.capture import ExportItemDraft, RetrievalCapture, parse_rag_context
from app.models import ChatMessage, ExportContextItem
from app.rag.rag_service import RagService
from app.repositories import (
    ChatMessageRepository,
    DocumentFileRepository,
    ExportContextRepository,
)
from app.services.confluence_service import ConfluenceService
from app.services.conversation_memory_service import ConversationMemoryService
from app.services.mistral_api_service import MistralApiService
from app.services.translation_service import TranslationService
from app.sk.chat_history_builder import build_agent_chat_history

logger = logging.getLogger(__name__)

_DEFAULT_EXPORT_CONTEXT_TTL_DAYS = 30


class ChatService:
    def __init__(
        self,
        chat_repo: ChatMessageRepository,
        mistral_service: MistralApiService,
        memory_service: ConversationMemoryService,
        translation_service: TranslationService,
        rag_service: RagService,
        semantic_kernel_factory: Any = None,
        confluence_service: ConfluenceService | None = None,
        max_history: int | None = None,
        export_repo: ExportContextRepository | None = None,
        document_file_repo: DocumentFileRepository | None = None,
        settings: Any | None = None,
    ) -> None:
        self._chat_repo = chat_repo
        self._mistral_service = mistral_service
        self._memory_service = memory_service
        self._translation_service = translation_service
        self._rag_service = rag_service
        # ``object`` with the SemanticKernelFactory interface: build_agent(...) and
        # run_agent(...). Kept as Any so this module never imports Semantic Kernel.
        self._semantic_kernel_factory: Any = semantic_kernel_factory
        self._confluence_service = confluence_service
        # Number of messages fed as runtime history to the agent (user+assistant rows).
        self._max_history = max_history if max_history else 20
        self._export_repo = export_repo
        self._document_file_repo = document_file_repo
        self._settings = settings

    # -- Turn processing ----------------------------------------------------------

    def process_user_message(self, session_id: str, user_message: str) -> ChatMessage:
        # 1. Save Original User Message to DB (source of truth)
        user_msg = ChatMessage(session_id=session_id, content=user_message, role="user")
        self._chat_repo.save(user_msg)

        if self._semantic_kernel_factory is not None:
            try:
                return self._process_with_knowledge_agent(session_id, user_message)
            except Exception:
                # Never lose a turn because the agent broke; fall back to the exact
                # augmented-prompt path the Java application used.
                logger.exception(
                    "Knowledge Generative Agent failed for session %s; using legacy path.",
                    session_id,
                )
        return self._process_with_legacy_prompt(session_id, user_message)

    # -- Knowledge Generative Agent path (Phase 1) --------------------------------

    def _process_with_knowledge_agent(self, session_id: str, user_message: str) -> ChatMessage:
        history = build_agent_chat_history(
            self._chat_repo.find_by_session_id(session_id),
            current_user_message=user_message,
            max_messages=self._max_history,
        )
        capture = RetrievalCapture()
        agent = self._semantic_kernel_factory.build_agent(
            rag_service=self._rag_service,
            session_id=session_id,
            confluence_service=self._confluence_service,
            capture=capture,
        )
        final_response = self._semantic_kernel_factory.run_agent(agent, history)

        self._memory_service.add_exchange(session_id, user_message, final_response)
        assistant = self._save_assistant_message(session_id, final_response)
        self._commit_export_context(session_id, assistant.id, capture)
        return assistant

    # -- Legacy augmented-prompt path ---------------------------------------------

    def _process_with_legacy_prompt(self, session_id: str, user_message: str) -> ChatMessage:
        # Standardizing on English for now
        user_lang_code = "en"

        # 2. Retrieve Knowledge Context (RAG)
        knowledge_context = self._rag_service.retrieve_context(user_message, session_id)
        capture = RetrievalCapture()
        _capture_rag_context(capture, knowledge_context)

        # 3. Retrieve Conversation History (Memory)
        history = self._memory_service.get_context(session_id)

        # 4. Construct Augmented Prompt
        parts: list[str] = []
        parts.append(
            "System: You are an advanced AI assistant powered by a Large Language Model architecture. "
            "You are helpful, professional, and capable of general reasoning as well as document-specific analysis.\n\n"
        )

        if knowledge_context and not knowledge_context.isspace():
            parts.append("DOCUMENTS CONTEXT:\n")
            parts.append(
                "The following information has been retrieved from the user's uploaded documents. "
                "Prioritize this information for accuracy if the user's question relates to it:\n"
            )
            parts.append(knowledge_context)
            parts.append("\n\n")

        parts.append("INSTRUCTIONS:\n")
        parts.append("- Answer the user's request accurately and conversationally.\n")
        parts.append(
            "- If DOCUMENTS CONTEXT is provided above, use it to ground your response.\n"
        )
        parts.append(
            "- If no context is provided, or the context doesn't contain the answer, rely on "
            "your extensive general knowledge to assist the user.\n"
        )
        parts.append(
            "- Do not explicitly mention the presence or absence of documents unless it is "
            "directly relevant to the user's query.\n\n"
        )

        for exchange in history:
            parts.append(f"User: {exchange.get('user', '')}\n")
            parts.append(f"Assistant: {exchange.get('assistant', '')}\n")
        parts.append(f"User: {user_message}")

        augmented_prompt = "".join(parts)

        # 5. Get AI Response
        final_response = self._mistral_service.generate_response(augmented_prompt)

        # 6. Store Exchange in Memory
        self._memory_service.add_exchange(session_id, user_message, final_response)

        # 7. Save Assistant Response to DB
        assistant = self._save_assistant_message(session_id, final_response, user_lang_code=user_lang_code)
        self._commit_export_context(session_id, assistant.id, capture)
        return assistant

    def _save_assistant_message(
        self, session_id: str, response: str, *, user_lang_code: str = "en"
    ) -> ChatMessage:
        ai_msg = ChatMessage(
            session_id=session_id,
            content=response,
            role="assistant",
            detected_language=user_lang_code,
            is_translated=False,
            timestamp=datetime.now(),
        )
        return self._chat_repo.save(ai_msg)

    def _commit_export_context(
        self, session_id: str, chat_message_id: int, capture: RetrievalCapture
    ) -> None:
        """Persist the turn's retrieved artifacts against the assistant message."""
        if self._export_repo is None:
            return
        items = capture.items
        if not items:
            return
        ttl_days = _DEFAULT_EXPORT_CONTEXT_TTL_DAYS
        if self._settings is not None:
            ttl_days = getattr(self._settings, "export_context_ttl_days", ttl_days)
        context = ExportContextRepository.create_context(chat_message_id, session_id)
        context.expires_at = datetime.now() + timedelta(days=ttl_days)
        context.source_count = len(items)
        self._export_repo.save(context)
        rows = [
            ExportContextItem(
                export_context_id=context.id,
                source_type=item.source_type,
                source_id=item.source_id,
                source_name=item.source_name,
                filename=item.filename,
                mime_type=item.mime_type,
                source_url=item.source_url,
                source_path=item.source_path,
                meta=item.metadata,
                retrieval_rank=item.retrieval_rank,
                retrieval_score=item.retrieval_score,
                content_reference=item.content_reference,
                export_strategy=item.export_strategy,
                native_format=item.native_format,
                size_bytes=item.size_bytes,
                exportable=item.exportable,
            )
            for item in items
        ]
        self._export_repo.save_items(rows)
        logger.info(
            "Export context recorded: session=%r chat_message_id=%s sources=%s",
            session_id,
            chat_message_id,
            len(rows),
        )

    def process_user_message_with_file(
        self,
        session_id: str,
        user_message: str,
        content: bytes,
        filename: str | None,
    ) -> ChatMessage:
        try:
            self._rag_service.ingest_document(content, filename, session_id)
        except Exception as exc:  # noqa: BLE001 - Java logs the error but proceeds with chat
            logger.error("Error ingesting document in chat: %s", exc)
        return self.process_user_message(session_id, user_message)

    def get_chat_history(self, session_id: str) -> list[ChatMessage]:
        return self._chat_repo.find_by_session_id(session_id)

    def get_recent_chat_sessions(self) -> list[ChatMessage]:
        return self._chat_repo.find_recent_chat_sessions()

    def delete_chat_session(self, session_id: str) -> None:
        self._chat_repo.delete_by_session_id(session_id)
        self._rag_service.delete_session(session_id)
        if self._export_repo is not None:
            self._export_repo.delete_by_session_id(session_id)
        if self._document_file_repo is not None:
            self._document_file_repo.delete_by_session_id(session_id)


def _capture_rag_context(capture: RetrievalCapture, knowledge_context: str) -> None:
    """Populate the turn's export capture from the legacy RAG context string."""
    if not knowledge_context or "[Source" not in knowledge_context:
        return
    for filename, body in parse_rag_context(knowledge_context):
        draft = ExportItemDraft(
            source_type="UPLOADED_DOCUMENT",
            source_id=filename,
            source_name=filename,
            filename=filename,
            native_format=filename.rsplit(".", 1)[-1].lower().strip(".") if "." in filename else None,
        )
        draft.merge_content(body)
        capture.add(draft)
