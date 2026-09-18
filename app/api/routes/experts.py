"""API route for the dashboard "Top Experts" (SME) card.

``GET /api/experts`` returns the subject-matter experts associated with the
knowledge sources retrieved for the *current query*. Experts come from the
actual source metadata captured for the session's latest assistant turn (or a
specific ``chatMessageId``); nothing is hard-coded or invented. When no person
metadata is available the list is empty.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_session_id, get_sme_expert_service
from app.schemas import ExpertsResponse
from app.services.sme_expert_service import SmeExpertService

router = APIRouter()


@router.get("", response_model=ExpertsResponse)
def get_experts(
    chat_message_id: int | None = Query(default=None, alias="chatMessageId"),
    session_id: str = Depends(get_session_id),
    service: SmeExpertService = Depends(get_sme_expert_service),
) -> ExpertsResponse:
    result = service.resolve(session_id, chat_message_id)
    return ExpertsResponse(**result)
