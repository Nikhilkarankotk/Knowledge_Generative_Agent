"""API routes for the detailed source-aware knowledge export.

* ``GET  /api/knowledge-export/{chat_message_id}`` - metadata of the export context
  recorded for an assistant message (status, resolved strategy, format, sources).
* ``POST /api/knowledge-export/{chat_message_id}`` - produce and download the export
  artifact (native file, generated document/report, or a sanitized ZIP with
  ``manifest.json``) from the *actual persisted retrieval context* of that message.
* ``POST /api/knowledge-export/{chat_message_id}/sources`` - download ONLY the
  source documents used to generate that one response (the chat "Export"
  button), organised by source system (``Confluence/``, ``GitHub/``,
  ``SharePoint/``...) + ``metadata.json``. Named ``confluence.zip`` /
  ``github.zip`` / ``sharepoint.zip`` when a single system was used, or
  ``Knowledge Export.zip`` when several were. No chat history or other
  responses' sources.
* ``POST /api/knowledge-export/session`` - download the whole chat session as one
  organized ``Export.zip`` (``ChatHistory/``, per-query ``Confluence/QueryN/``
  folders with each page written once, and a ``metadata.json`` query->source map).

Only a ``chat_message_id`` is ever accepted - never file paths, URLs or format
strings from the client. ``X-Session-ID`` must match the message's session.
The lightweight per-answer TXT export lives under ``/api/export`` (see
:mod:`app.api.routes.export`).
"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse

from app.api.dependencies import (
    get_chat_repository,
    get_export_service,
    get_session_id,
)
from app.api.export_utils import content_disposition, export_error_response
from app.export.errors import ExportError
from app.export.export_service import ExportService
from app.repositories import ChatMessageRepository

router = APIRouter()


class ExportRequest(BaseModel):
    hint: str = ""


class ExportSourceOut(BaseModel):
    source_type: str
    source_name: str
    source_id: str | None = None
    filename: str | None = None
    retrieval_rank: int | None = None
    exportable: bool = True


class ExportMetadataOut(BaseModel):
    chat_message_id: int
    status: str | None = None
    strategy: str | None = None
    requested_format: str | None = None
    format_reason: str | None = None
    source_count: int
    sources: list[ExportSourceOut]


def _metadata_response(
    chat_message_id: int, session_id: str, export_service: ExportService
) -> ExportMetadataOut:
    context, items = export_service.load_context(chat_message_id, session_id)
    return ExportMetadataOut(
        chat_message_id=chat_message_id,
        status=context.status,
        strategy=context.strategy,
        requested_format=context.requested_format,
        format_reason=context.format_reason,
        source_count=len(items),
        sources=[
            ExportSourceOut(
                source_type=item.source_type or "UNKNOWN",
                source_name=item.source_name or item.source_id or "",
                source_id=item.source_id,
                filename=item.filename,
                retrieval_rank=item.retrieval_rank,
                exportable=bool(item.exportable),
            )
            for item in items
        ],
    )


def _artifact_response(artifact) -> Response:  # noqa: ANN001 - ExportArtifact
    """Turn an ExportArtifact into a download response (bytes or temp file)."""
    headers = {
        "Content-Disposition": content_disposition(artifact.filename),
        "X-Export-Filename": artifact.filename,
    }
    if artifact.data is not None:
        return Response(content=artifact.data, media_type=artifact.mime_type, headers=headers)
    if artifact.path is not None:
        background: BackgroundTask | None = None
        if artifact.cleanup_dir:
            background = BackgroundTask(shutil.rmtree, artifact.cleanup_dir, ignore_errors=True)
        return FileResponse(
            artifact.path,
            media_type=artifact.mime_type,
            filename=artifact.filename,
            headers=headers,
            background=background,
        )
    return JSONResponse(status_code=500, content={"message": "Export produced no artifact."})


@router.post("/session")
async def download_session_export(
    session_id: str = Depends(get_session_id),
    export_service: ExportService = Depends(get_export_service),
) -> Response:
    """Download the whole chat session as one organized ``Export.zip``.

    ``ChatHistory/QueryN_Response.docx`` holds each question + answer;
    ``Confluence/QueryN/<Page>.docx`` holds only the pages used for that answer
    (each page written once across the archive); ``metadata.json`` maps every
    query to its source documents. Scoped by ``X-Session-ID``.
    """
    try:
        artifact = await run_in_threadpool(export_service.export_session, session_id)
    except ExportError as exc:
        return export_error_response(exc)
    return _artifact_response(artifact)


_NO_SOURCES_MESSAGE = (
    "No source documents (Confluence, GitHub, SharePoint or uploads) were used to "
    "generate a response in this chat yet."
)


async def _export_sources_for(
    export_service: ExportService,
    session_id: str,
    preferred_chat_message_id: int | None,
) -> Response:
    """Export the Confluence sources of the preferred response, falling back to the
    most recent response in the session that actually has exportable sources."""
    try:
        target = await run_in_threadpool(
            export_service.find_latest_exportable_message,
            session_id,
            preferred_chat_message_id=preferred_chat_message_id,
        )
        if target is None:
            return JSONResponse(status_code=404, content={"message": _NO_SOURCES_MESSAGE})
        artifact = await run_in_threadpool(
            export_service.export_response_sources, target, session_id
        )
    except ExportError as exc:
        return export_error_response(exc)
    return _artifact_response(artifact)


@router.post("/latest/sources")
async def download_latest_response_sources(
    session_id: str = Depends(get_session_id),
    export_service: ExportService = Depends(get_export_service),
) -> Response:
    """Download the source documents behind the most recent exportable response.

    The server picks the newest assistant response in this session that actually
    used retrieved sources, so the chat "Export" button keeps working after a
    page refresh even when the very last answer used none.
    """
    return await _export_sources_for(export_service, session_id, None)


@router.post("/{chat_message_id}/sources")
async def download_response_sources(
    chat_message_id: int,
    session_id: str = Depends(get_session_id),
    export_service: ExportService = Depends(get_export_service),
) -> Response:
    """Download ONLY the source documents used to generate one AI response.

    The ZIP is organised by source system (``Confluence/``, ``GitHub/``,
    ``SharePoint/``, ``UploadedDocuments/``) plus a ``metadata.json``, and is
    named ``confluence.zip`` / ``github.zip`` / ``sharepoint.zip`` when a single
    system was used or ``Knowledge Export.zip`` when several were. No chat
    history, no earlier queries and no other responses' sources are included. If
    the given response has no sources, the most recent response in the session
    that does is exported instead. Scoped by ``X-Session-ID``.
    """
    return await _export_sources_for(export_service, session_id, chat_message_id)


@router.get("/{chat_message_id}", response_model=ExportMetadataOut)
async def get_export_metadata(
    chat_message_id: int,
    session_id: str = Depends(get_session_id),
    export_service: ExportService = Depends(get_export_service),
) -> ExportMetadataOut | Response:
    try:
        return await run_in_threadpool(
            _metadata_response, chat_message_id, session_id, export_service
        )
    except ExportError as exc:
        return export_error_response(exc)


@router.post("/{chat_message_id}")
async def download_knowledge_export(
    request: Request,
    chat_message_id: int,
    session_id: str = Depends(get_session_id),
    export_service: ExportService = Depends(get_export_service),
    chat_repo: ChatMessageRepository = Depends(get_chat_repository),
) -> Response:
    hint = ""
    try:
        body = await request.json()
        export_request = ExportRequest.model_validate(body or {})
        hint = export_request.hint
    except Exception:  # noqa: BLE001 - optional hint; never fail the export
        hint = ""

    assistant_text = ""
    message = chat_repo.find_by_id(chat_message_id)
    if message is not None and message.role == "assistant":
        assistant_text = message.content or ""

    try:
        artifact = await run_in_threadpool(
            export_service.export,
            chat_message_id,
            session_id,
            assistant_text=assistant_text,
            user_hint=hint,
        )
    except ExportError as exc:
        return export_error_response(exc)
    return _artifact_response(artifact)
