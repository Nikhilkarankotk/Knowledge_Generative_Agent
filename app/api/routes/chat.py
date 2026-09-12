"""Equivalent of ``ChatController.java``.

Endpoints:

* ``POST /api/chat`` (application/json and multipart/form-data)
* ``GET  /api/history``
* ``GET  /api/history/sessions``
* ``DELETE /api/history/sessions/{sessionId}``

The ``X-Session-ID`` header is optional and defaults to ``default-session``.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, Request, Response, UploadFile, status
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.dependencies import get_chat_service, get_session_id
from app.schemas import ChatMessageOut, ChatRequest
from app.services.chat_service import ChatService

router = APIRouter()


@router.post("/chat", response_model=ChatMessageOut)
async def handle_message(
    request: Request,
    session_id: str = Depends(get_session_id),
    chat_service: ChatService = Depends(get_chat_service),
) -> ChatMessageOut | Response:
    content_type = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in content_type:
        form = await request.form()
        message_part = form.get("message")
        pdf_part = form.get("pdf")
        if message_part is None:
            return Response(content="Required part 'message' is not present.", status_code=400)
        if pdf_part is None:
            return Response(content="Required part 'pdf' is not present.", status_code=400)
        message = str(message_part)
        pdf_file = cast(UploadFile, pdf_part)
        content = await pdf_file.read()
        filename = pdf_file.filename
        saved = await run_in_threadpool(
            chat_service.process_user_message_with_file, session_id, message, content, filename
        )
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - malformed JSON body
            return Response(content="Request body is not valid JSON.", status_code=400)
        try:
            chat_request = ChatRequest.model_validate(body)
        except ValidationError:
            return Response(content="Required request body is missing or invalid.", status_code=400)
        saved = await run_in_threadpool(
            chat_service.process_user_message, session_id, chat_request.message
        )
    return ChatMessageOut.from_message(saved)


@router.get("/history", response_model=list[ChatMessageOut])
async def get_history(
    session_id: str = Depends(get_session_id),
    chat_service: ChatService = Depends(get_chat_service),
) -> list[ChatMessageOut]:
    messages = await run_in_threadpool(chat_service.get_chat_history, session_id)
    return [ChatMessageOut.from_message(message) for message in messages]


@router.get("/history/sessions", response_model=list[ChatMessageOut])
async def get_sessions(
    chat_service: ChatService = Depends(get_chat_service),
) -> list[ChatMessageOut]:
    messages = await run_in_threadpool(chat_service.get_recent_chat_sessions)
    return [ChatMessageOut.from_message(message) for message in messages]


@router.delete("/history/sessions/{session_id}", status_code=status.HTTP_200_OK)
async def delete_session(
    session_id: str,
    chat_service: ChatService = Depends(get_chat_service),
) -> Response:
    await run_in_threadpool(chat_service.delete_chat_session, session_id)
    return Response(status_code=status.HTTP_200_OK)
