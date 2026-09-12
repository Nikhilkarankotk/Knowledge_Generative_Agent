# Migration Status — Java → Python

All components of the original Spring Boot chatbot have been migrated.
Tests, linting (`ruff`) and type-checking (`mypy`) all pass.

## Component mapping

| Java component | Python equivalent | Status |
|---|---|---|
| `ChatController` | `app/api/routes/chat.py` | ✅ Done |
| `RagController` | `app/api/routes/rag.py` | ✅ Done |
| `MistralController` | `app/api/routes/mistral.py` | ✅ Done |
| `FeedbackController` | `app/api/routes/feedback.py` | ✅ Done |
| `ChatService` | `app/services/chat_service.py` | ✅ Done |
| `RagService` | `app/rag/rag_service.py` | ✅ Done |
| `EmbeddingService` | `app/rag/embedding_service.py` | ✅ Done |
| `TextChunker` | `app/rag/text_chunker.py` | ✅ Done |
| `DocumentParser` | `app/rag/document_parser.py` | ✅ Done |
| `MistralApiService` | `app/services/mistral_api_service.py` | ✅ Done |
| `MistralService` | `app/services/mistral_service.py` | ✅ Done |
| `ConversationMemoryService` | `app/services/conversation_memory_service.py` | ✅ Done |
| `TranslationService` | `app/services/translation_service.py` | ✅ Done |
| `ChatMessageConverter` | `app/rag/converters.py` | ✅ Done |
| `ChatMessage` entity | `app/models/chat_message.py` | ✅ Done |
| `DocumentChunk` entity | `app/models/document_chunk.py` | ✅ Done |
| `Feedback` entity | `app/models/feedback.py` | ✅ Done |
| `ChatMessageRepository` | `app/repositories/chat_message_repository.py` | ✅ Done |
| `DocumentChunkRepository` | `app/repositories/document_chunk_repository.py` | ✅ Done |
| `FeedbackRepository` | `app/repositories/feedback_repository.py` | ✅ Done |
| `ChatRequest` DTO | `app/schemas/chat.py` | ✅ Done |
| `TranslationResult` DTO | `app/schemas/translation.py` | ✅ Done |
| `DatabaseConfig` | `app/core/database.py` | ✅ Done |
| `CorsConfig` | `app/main.py` | ✅ Done |
| `ChatMessageServiceImpl` (entity) | `app/schemas/chat.py` (Jackson field names) | ✅ Done |
| `application.yml` | `app/core/config.py` | ✅ Done |
| Swagger/OpenAPI | FastAPI built-in | ✅ Done |
| `rag-controller.js` (frontend API) | FastAPI endpoints match `api.ts` contract | ✅ Done |

## Known deviations

| Area | Deviation | Rationale |
|---|---|---|
| Embeddings storage | JSON column on `document_chunk` (vs Java `@ElementCollection` table) | Simpler for SQLite; no join table needed for tests |
| Postgres vector column | `EmbeddingType` TypeDecorator: pgvector `Vector` on Postgres, `JSON` on SQLite | Matches the live `vector` column; SQLite fallback for tests |
| BigInteger PKs | `BigInteger().with_variant(Integer, "sqlite")` | SQLite autoincrement requires Integer PKs |
| Error wrapping | `_parse_pdf`/`_parse_docx` wrap all exceptions in `IllegalArgumentException` | Matches Java's `IOException` → 500 contract |
| OCR endpoint | Java's retired `POST /v1/ocr/process` → modern flow: `GET /v1/files/{id}/url` relies on signed URL, `POST /v1/ocr`, joins `pages[].markdown` | Java endpoint returns 404 against current Mistral API |

## Test suite

```
83 passed
```
