"""API routes for the lightweight per-answer AI response export.

* ``POST /api/export/{chat_message_id}`` - a plain-text (``text/plain``) transcript
  of the AI response. It is built from data already persisted at chat time - the
  assistant ``chat_message`` row, the preceding user message in the same session,
  and (when recorded) the retrieval-context sources for that answer. It never
  re-runs retrieval, never analyzes a repository and never produces a ZIP.

Only a ``chat_message_id`` is ever accepted. ``X-Session-ID`` must match the
message's session (session isolation). The detailed source-aware ZIP export lives
under ``/api/knowledge-export`` (see :mod:`app.api.routes.knowledge_export`).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response
from starlette.responses import JSONResponse

from app.api.dependencies import (
    get_chat_repository,
    get_export_context_repository,
    get_session_id,
)
from app.api.export_utils import content_disposition
from app.export.response_export import (
    ResponseExportSource,
    render_response_txt,
    response_export_filename,
)
from app.repositories import ChatMessageRepository, ExportContextRepository

router = APIRouter()


@router.post("/{chat_message_id}")
def download_response_export(
    chat_message_id: int,
    session_id: str = Depends(get_session_id),
    chat_repo: ChatMessageRepository = Depends(get_chat_repository),
    export_repo: ExportContextRepository = Depends(get_export_context_repository),
) -> Response:
    message = chat_repo.find_by_id(chat_message_id)
    if message is None or message.role != "assistant":
        return JSONResponse(
            status_code=404,
            content={
                "message": f"No assistant response was found for chat message {chat_message_id}."
            },
        )
    if (message.session_id or "") != session_id:
        return JSONResponse(
            status_code=403,
            content={"message": "This export belongs to a different chat session."},
        )

    user_message = chat_repo.find_last_user_message(session_id, chat_message_id)
    user_query = ""
    if user_message is not None and user_message.content:
        user_query = user_message.content
    sources = tuple(
        ResponseExportSource(
            source_type=(source.source_type or "UNKNOWN"),
            source_name=(source.source_name or source.source_id or ""),
            source_id=source.source_id,
            source_url=source.source_url,
        )
        for source in export_repo.find_items_by_chat_message_id(chat_message_id)
    )

    transcript = render_response_txt(
        user_query=user_query,
        assistant_response=message.content or "",
        timestamp=message.timestamp or datetime.now(),
        sources=sources,
        exported_at=datetime.now(),
    )
    filename = response_export_filename(chat_message_id)
    return Response(
        content=transcript.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": content_disposition(filename),
            "X-Export-Filename": filename,
        },
    )
