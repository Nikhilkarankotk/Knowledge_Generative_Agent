"""Equivalent of ``MistralController.java``.

Endpoint: ``POST /api/mistral/upload`` (multipart: ``file`` + ``message``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import get_mistral_service
from app.services.mistral_service import MistralService

router = APIRouter()


@router.post("/upload")
async def upload_file_and_message(
    file: UploadFile = File(..., description="Document to send to Mistral OCR"),
    message: str = Form(..., description="Message/instruction for the OCR model"),
    mistral_service: MistralService = Depends(get_mistral_service),
) -> Response:
    try:
        content = await file.read()
        response_text = await run_in_threadpool(
            mistral_service.upload_document_and_send_message,
            file.filename or "upload",
            content,
            message,
        )
        return Response(content=response_text)
    except Exception as exc:  # noqa: BLE001 - Java wraps every failure as 500
        return Response(content=f"Error: {exc}", status_code=500)
