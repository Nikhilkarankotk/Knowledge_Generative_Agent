"""API routes for the detailed source-aware knowledge export.

* ``GET  /api/knowledge-export/{chat_message_id}`` - metadata of the export context
  recorded for an assistant message (status, resolved strategy, format, sources).
* ``POST /api/knowledge-export/{chat_message_id}`` - produce and download the export
  artifact (native file, generated document/report, or a sanitized ZIP with
  ``manifest.json``) from the *actual persisted retrieval context* of that message.

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

    headers = {
        "Content-Disposition": content_disposition(artifact.filename),
        "X-Export-Filename": artifact.filename,
    }
    if artifact.data is not None:
        return Response(
            content=artifact.data,
            media_type=artifact.mime_type,
            headers=headers,
        )
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
