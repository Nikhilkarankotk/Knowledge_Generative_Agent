"""Equivalent of ``ChatService.java``.

Preserves the exact augmented-prompt construction used by the Java application:
system preamble, optional DOCUMENTS CONTEXT block, INSTRUCTIONS block, conversation
history exchanges, and the final user message - in that order.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.models import ChatMessage
from app.rag.rag_service import RagService
from app.repositories import ChatMessageRepository
from app.services.conversation_memory_service import ConversationMemoryService
from app.services.mistral_api_service import MistralApiService
from app.services.translation_service import TranslationService

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self,
        chat_repo: ChatMessageRepository,
        mistral_service: MistralApiService,
        memory_service: ConversationMemoryService,
        translation_service: TranslationService,
        rag_service: RagService,
    ) -> None:
        self._chat_repo = chat_repo
        self._mistral_service = mistral_service
        self._memory_service = memory_service
        self._translation_service = translation_service
        self._rag_service = rag_service

    def process_user_message(self, session_id: str, user_message: str) -> ChatMessage:
        # Standardizing on English for now
        user_lang_code = "en"

        # 1. Save Original User Message to DB
        user_msg = ChatMessage(session_id=session_id, content=user_message, role="user")
        self._chat_repo.save(user_msg)

        # 2. Retrieve Knowledge Context (RAG)
        knowledge_context = self._rag_service.retrieve_context(user_message, session_id)

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
        ai_msg = ChatMessage(
            session_id=session_id,
            content=final_response,
            role="assistant",
            detected_language=user_lang_code,
            is_translated=False,
            timestamp=datetime.now(),
        )
        return self._chat_repo.save(ai_msg)

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
