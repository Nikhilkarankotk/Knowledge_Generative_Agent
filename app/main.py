"""FastAPI application entry point.

Equivalent of ``ChatbotApplication.java`` (+ ``CorsConfig`` and the ``DatabaseConfig``
startup initializer). Mounts the same API surface under the same paths as the Spring
Boot application and applies the same permissive CORS rules for ``http://localhost:4200``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.dependencies import _client, _database
from app.api.routes import chat, feedback, mistral, rag
from app.core.config import get_settings
from app.core.exceptions import ChatbotError, UnsupportedFileTypeError
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)
settings = get_settings()
configure_logging(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Mirrors DatabaseConfig.dataSourceInitializer: ensure pgvector extension exists and
    # (like Hibernate ddl-auto=update) that the schema is present.
    _database.init_schema()
    logger.info("Application started: %s", settings.app_name)
    yield
    _client.close()
    logger.info("Application shutting down")


app = FastAPI(
    title="Portfolio Chatbot",
    description="Python migration of the Java Portfolio Chatbot (hybrid RAG assistant).",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - equivalent of CorsConfig.java
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(UnsupportedFileTypeError)
async def unsupported_file_type_handler(_: Request, exc: UnsupportedFileTypeError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"message": str(exc)})


@app.exception_handler(ChatbotError)
async def chatbot_error_handler(_: Request, exc: ChatbotError) -> JSONResponse:
    # Java: uncaught RuntimeException -> HTTP 500 with a message body.
    return JSONResponse(status_code=500, content={"message": str(exc)})


# Equivalent of @RequestMapping("/api") on ChatController plus the other controllers.
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(rag.router, prefix="/api/rag", tags=["rag"])
app.include_router(mistral.router, prefix="/api/mistral", tags=["mistral"])
app.include_router(feedback.router, prefix="/api/feedback", tags=["feedback"])


@app.get("/health", tags=["health"])
def health() -> dict[str, str]:
    return {"status": "ok"}
