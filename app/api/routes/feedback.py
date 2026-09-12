"""Equivalent of ``FeedbackController.java``.

Endpoint: ``POST /api/feedback`` with body ``{messageId, rating, correctedAnswer}``.

If the referenced chat message does not exist the Java code raised a
``RuntimeException("Message not found")`` mapped to HTTP 500; the same behaviour is
preserved via the global exception handler registered in :mod:`app.main`.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response

from app.api.dependencies import get_chat_repository, get_feedback_repository
from app.core.exceptions import ChatMessageNotFoundException
from app.models import Feedback
from app.repositories import ChatMessageRepository, FeedbackRepository
from app.schemas import FeedbackRequest

router = APIRouter()


@router.post("", status_code=200)
def submit_feedback(
    request: FeedbackRequest,
    chat_repo: ChatMessageRepository = Depends(get_chat_repository),
    feedback_repo: FeedbackRepository = Depends(get_feedback_repository),
) -> Response:
    message = chat_repo.find_by_id(request.messageId)
    if message is None:
        raise ChatMessageNotFoundException("Message not found")

    feedback = feedback_repo.find_by_message_id(request.messageId)
    if feedback is None:
        feedback = Feedback(message_id=request.messageId, timestamp=datetime.now())

    feedback.rating = request.rating
    feedback.corrected_answer = request.correctedAnswer
    feedback_repo.save(feedback)
    return Response(status_code=200)
