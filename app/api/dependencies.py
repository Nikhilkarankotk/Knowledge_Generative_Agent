"""FastAPI dependency wiring.

Builds the application's service graph. The Java application relies on Spring's
constructor injection; here we compose the same graph using FastAPI dependencies.

Singletons (client, memory) live for the process lifetime; anything bound to a database
session (repositories/services) is constructed per request.
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.database import Database
from app.export.export_service import ExportService
from app.export.intent import IntentRecommender
from app.export.sources import ExportLimits, ExportServices
from app.llm import MistralClient, build_report_llm_service
from app.rag.document_parser import DocumentParser
from app.rag.embedding_service import EmbeddingService
from app.rag.rag_service import RagService
from app.rag.text_chunker import TextChunker
from app.repositories import (
    ChatMessageRepository,
    DocumentChunkRepository,
    DocumentFileRepository,
    DocumentRepository,
    ExportContextRepository,
    FeedbackRepository,
)
from app.services.chat_service import ChatService
from app.services.confluence_service import ConfluenceService
from app.services.conversation_memory_service import ConversationMemoryService
from app.services.github_service import GitHubService
from app.services.mistral_api_service import MistralApiService
from app.services.mistral_service import MistralService
from app.services.sharepoint_service import SharePointService
from app.services.translation_service import TranslationService

DEFAULT_SESSION_ID = "default-session"

logger = logging.getLogger(__name__)


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
    ConfluenceService | None,
    GitHubService | None,
    SharePointService | None,
    object | None,
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
    confluence_service = ConfluenceService.from_settings(settings)
    github_service = GitHubService.from_settings(settings)
    sharepoint_service = SharePointService.from_settings(settings)
    semantic_kernel_factory = None
    if settings.sk_agent_enabled:
        from app.sk import SemanticKernelFactory

        semantic_kernel_factory = SemanticKernelFactory(
            settings,
            confluence_service=confluence_service,
            github_service=github_service,
            sharepoint_service=sharepoint_service,
            use_loop=True,
        )
        logger.info(
            "Semantic Kernel agent enabled (model=%s, base_url=%s)",
            settings.mistral_chat_model,
            settings.mistral_base_url,
        )
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
        confluence_service,
        github_service,
        sharepoint_service,
        semantic_kernel_factory,
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
    _confluence_service,
    _github_service,
    _sharepoint_service,
    _semantic_kernel_factory,
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


def get_export_context_repository(db: Session = Depends(get_db)) -> ExportContextRepository:
    return ExportContextRepository(db)


def get_document_file_repository(db: Session = Depends(get_db)) -> DocumentFileRepository:
    return DocumentFileRepository(db)


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
    document_file_repo: DocumentFileRepository = Depends(get_document_file_repository),
) -> RagService:
    return RagService.build(
        document_parser=_document_parser,
        text_chunker=_text_chunker,
        embedding_service=_embedding_service,
        chunk_repo=chunk_repo,
        mistral_api_service=_mistral_api_service,
        settings=_settings,
        document_repo=document_repo,
        document_file_repo=document_file_repo,
    )


def get_chat_service(
    chat_repo: ChatMessageRepository = Depends(get_chat_repository),
    rag_service: RagService = Depends(get_rag_service),
    export_repo: ExportContextRepository = Depends(get_export_context_repository),
    document_file_repo: DocumentFileRepository = Depends(get_document_file_repository),
) -> ChatService:
    return ChatService(
        chat_repo=chat_repo,
        mistral_service=_mistral_api_service,
        memory_service=_memory_service,
        translation_service=_translation_service,
        rag_service=rag_service,
        semantic_kernel_factory=_semantic_kernel_factory,
        confluence_service=_confluence_service,
        max_history=_settings.conversation_max_history * 2,
        export_repo=export_repo,
        document_file_repo=document_file_repo,
        settings=_settings,
    )


def get_export_service(
    export_repo: ExportContextRepository = Depends(get_export_context_repository),
    document_file_repo: DocumentFileRepository = Depends(get_document_file_repository),
) -> Generator[ExportService, None, None]:
    services = ExportServices(
        document_file_repo=document_file_repo,
        confluence_service=_confluence_service,
        github_service=_github_service,
        sharepoint_service=_sharepoint_service,
    )
    recommender = IntentRecommender(
        _mistral_api_service, enabled=_settings.export_intent_recommendation_enabled
    )
    from app.export.synthesis import ReportSynthesizer

    # The narrative synthesis gets its own bounded LLM client so writing the full
    # report has room while a slow call cannot stall the export for minutes.
    # GITHUB_LLM_PROVIDER=openrouter routes this through OpenRouter (separate LLM
    # for the GitHub report); otherwise the default Mistral synthesis client is used.
    synthesis_service = build_report_llm_service(_settings)
    synthesizer = ReportSynthesizer(
        synthesis_service,
        enabled=_settings.export_report_synthesis_enabled,
        max_evidence_chars=_settings.github_analysis_max_context_chars,
    )
    export_service = ExportService(
        export_repo=export_repo,
        settings=_settings,
        services=services,
        recommender=recommender,
        synthesizer=synthesizer,
        limits=ExportLimits.from_settings(_settings),
    )
    try:
        yield export_service
    finally:
        # The synthesis backend owns a dedicated per-request HTTP client; close it
        # so idle connections do not accumulate (OpenRouter or Mistral).
        close = getattr(synthesis_service, "close", None)
        if callable(close):
            close()
