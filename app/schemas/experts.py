"""Schemas for the dashboard Top Experts (SME) card."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExpertOut(BaseModel):
    name: str
    role: str
    source: str
    sourceCount: int
    reason: str
    initials: str = ""
    sources: list[str] = Field(default_factory=list)


class ExpertsResponse(BaseModel):
    chatMessageId: int | None = None
    experts: list[ExpertOut] = Field(default_factory=list)
