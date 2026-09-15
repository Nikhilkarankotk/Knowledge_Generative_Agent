"""Lightweight per-answer TXT export builder.

The per-message "Export" action produces a plain-text transcript built entirely
from data already persisted at chat time:

* the assistant ``chat_message`` row (the AI answer and its timestamp),
* the immediately preceding user message in the same session,
* the persisted retrieval context attached to that answer (source names/types/URLs)
  when one was recorded.

This module is a pure formatting layer with no database, service or plugin
dependencies - it never re-runs RAG, Confluence, GitHub or SharePoint, never
analyzes a repository and never produces a ZIP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.export.formats import safe_filename


@dataclass(frozen=True)
class ResponseExportSource:
    """A display-only summary of a persisted retrieval artifact."""

    source_type: str
    source_name: str
    source_id: str | None = None
    source_url: str | None = None


def response_export_filename(chat_message_id: int) -> str:
    """Return the safe download filename for a response export."""
    safe = safe_filename(str(chat_message_id), default="response")
    return f"ai-response-{safe}.txt"


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def render_response_txt(
    *,
    user_query: str,
    assistant_response: str,
    timestamp: datetime | None = None,
    sources: tuple[ResponseExportSource, ...] = (),
    exported_at: datetime | None = None,
) -> str:
    """Render the plain-text transcript for an AI response export.

    ``sources`` are included only when non-empty - a response with no recorded
    retrieval context still exports successfully (no sources section).
    """
    lines: list[str] = []
    lines.append("AI Response Export")
    lines.append("=" * 20)
    lines.append(f"Date: {_format_timestamp(timestamp)}")
    if exported_at is not None:
        lines.append(f"Exported: {_format_timestamp(exported_at)}")
    lines.append("")
    lines.append("User Query:")
    lines.append((user_query or "").strip() or "-")
    lines.append("")
    lines.append("AI Response:")
    lines.append((assistant_response or "").strip() or "-")
    if sources:
        lines.append("")
        lines.append("Sources:")
        for source in sources:
            label = source.source_name or source.source_id or source.source_type
            entry = f"- {source.source_type}: {label}"
            if source.source_url:
                entry = f"{entry} ({source.source_url})"
            lines.append(entry)
    lines.append("")
    return "\n".join(lines)
