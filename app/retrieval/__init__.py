"""Shared retrieval plumbing used by every knowledge-source plugin."""

from __future__ import annotations

from .state import (
    MAX_EVIDENCE_ITEMS,
    STATE_CONTENT_LIMIT,
    RetrievalLedger,
    append_state,
    match_known_scope,
    state_end,
    state_start,
    strip_retrieval_state,
)

__all__ = [
    "MAX_EVIDENCE_ITEMS",
    "STATE_CONTENT_LIMIT",
    "RetrievalLedger",
    "append_state",
    "match_known_scope",
    "state_end",
    "state_start",
    "strip_retrieval_state",
]
