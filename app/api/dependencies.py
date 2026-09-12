"""FastAPI dependency wiring.

Builds the application's service graph. The Java application relies on Spring's
constructor injection; here we compose the same graph using FastAPI dependencies.

Singletons (client, memory) live for the process lifetime; anything bound to a database
session (repositories/services) is constructed per request.
"""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import Database
from app.llm import MistralClient
from app.rag.document_parser import DocumentParser
from app.rag.embedding_service import EmbeddingService
from app.rag.rag_service import RagService
from app.rag.text_chunker import TextChunker
from app.repositories import (
    ChatMessageRepository,
    DocumentChunkRepository,
    DocumentRepository,
    FeedbackRepository,
)
from app.services.chat_service import ChatService
from app.services.conversation_memory_service import ConversationMemoryService
from app.services.mistral_api_service import MistralApiService
from app.services.mistral_service import MistralService
from app.services.translation_service import TranslationService

DEFAULT_SESSION_ID = "default-session"


# --- Process-level singletons ----------------------------------------------------------

def _build_singletons() -> tuple[
    Settings,
    MistralClient,
    MistralApiService,
    MistralService,
    ConversationMemoryService,
    TranslationService,
    DocumentParser,
    TextChunker,
    EmbeddingService,
    Database,
]:
    settings = get_settings()
    client = MistralClient.from_settings(settings)
    mistral_api_service = MistralApiService(client)
    mistral_service = MistralService(client)
    memory_service = ConversationMemoryService.from_settings(settings)
    translation_service = TranslationService(mistral_api_service)
    document_parser = DocumentParser()
    text_chunker = TextChunker()
    embedding_service = EmbeddingService.from_client(client)
    database = Database(settings)
    return (
        settings,
        client,
        mistral_api_service,
        mistral_service,
        memory_service,
        translation_service,
        document_parser,
        text_chunker,
        embedding_service,
        database,
    )


(
    _settings,
    _client,
    _mistral_api_service,
    _mistral_service,
    _memory_service,
    _translation_service,
    _document_parser,
    _text_chunker,
    _embedding_service,
    _database,
) = _build_singletons()


# --- DB session ------------------------------------------------------------------------

def get_db() -> Generator[Session, None, None]:
    session = _database.create_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --- Session header --------------------------------------------------------------------

def get_session_id(
    x_session_id: str = Header(
        default="default-session",
        alias="X-Session-ID",
        description="Identifies the chat session; defaults to 'default-session'.",
    ),
) -> str:
    return x_session_id


# --- Repositories ----------------------------------------------------------------------

def get_chat_repository(db: Session = Depends(get_db)) -> ChatMessageRepository:
    return ChatMessageRepository(db)


def get_document_chunk_repository(db: Session = Depends(get_db)) -> DocumentChunkRepository:
    return DocumentChunkRepository(db)


def get_document_repository(db: Session = Depends(get_db)) -> DocumentRepository:
    return DocumentRepository(db)


def get_feedback_repository(db: Session = Depends(get_db)) -> FeedbackRepository:
    return FeedbackRepository(db)


# --- Services --------------------------------------------------------------------------

def get_mistral_api_service() -> MistralApiService:
    return _mistral_api_service


def get_mistral_service() -> MistralService:
    return _mistral_service


def get_memory_service() -> ConversationMemoryService:
    return _memory_service


def get_translation_service() -> TranslationService:
    return _translation_service


def get_rag_service(
    chunk_repo: DocumentChunkRepository = Depends(get_document_chunk_repository),
    document_repo: DocumentRepository = Depends(get_document_repository),
) -> RagService:
    return RagService.build(
        document_parser=_document_parser,
        text_chunker=_text_chunker,
        embedding_service=_embedding_service,
        chunk_repo=chunk_repo,
        mistral_api_service=_mistral_api_service,
        settings=_settings,
        document_repo=document_repo,
    )


def get_chat_service(
    chat_repo: ChatMessageRepository = Depends(get_chat_repository),
    rag_service: RagService = Depends(get_rag_service),
) -> ChatService:
    return ChatService(
        chat_repo=chat_repo,
        mistral_service=_mistral_api_service,
        memory_service=_memory_service,
        translation_service=_translation_service,
        rag_service=rag_service,
    )
