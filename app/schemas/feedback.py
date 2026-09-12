"""Equivalent of ``FeedbackRequest.java``."""


from pydantic import BaseModel


class FeedbackRequest(BaseModel):
    messageId: int
    rating: int | None = None
    correctedAnswer: str | None = None
