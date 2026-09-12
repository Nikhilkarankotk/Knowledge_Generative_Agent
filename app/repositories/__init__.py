"""Repository equivalents for the Spring Data JPA repositories."""

from app.repositories.chat_message_repository import ChatMessageRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.document_repository import DocumentRepository
from app.repositories.feedback_repository import FeedbackRepository

__all__ = [
    "ChatMessageRepository",
    "DocumentChunkRepository",
    "DocumentRepository",
    "FeedbackRepository",
]
