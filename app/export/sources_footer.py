"""Render the "Sources" footer appended to every grounded AI response.

After a chat turn the :class:`~app.export.capture.RetrievalCapture` knows exactly
which documents backed the answer (the items marked ``exportable`` - the same set
the Export button downloads). This module turns that set into a compact Markdown
section listing each document with a clickable link to where it was retrieved
from, grouped by knowledge source::

    ---
    **Sources**
    - **Confluence:** [Netflix System Design and Architecture](https://.../pages/622593)
    - **SharePoint:** [Job_Portal_Web_Application CICD Pipeline flow.pdf](https://...)
    - **GitHub:** [nikhilkarankotk/Job_Portal_Web_Application](https://github.com/...)
    - **Uploaded document:** Netflix CICD Pipeline flow.pdf

The footer is derived from the retrieval capture - never from the model's own
text - so it can only ever name documents that were actually retrieved.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.export.capture import ExportItemDraft

_SOURCES_HEADING = "**Sources**"
_RULE = "---"

# Display order and labels of the knowledge sources in the footer.
_SOURCE_LABELS: dict[str, str] = {
    "UPLOADED_DOCUMENT": "Uploaded document",
    "CONFLUENCE": "Confluence",
    "SHAREPOINT": "SharePoint",
    "GITHUB": "GitHub",
}
_SOURCE_ORDER: tuple[str, ...] = ("UPLOADED_DOCUMENT", "CONFLUENCE", "SHAREPOINT", "GITHUB")

_FOOTER_RE = re.compile(r"\n*---\s*\n\*\*Sources\*\*\s*\n(?:- .*\n?)*\s*$", re.MULTILINE)


def has_sources_footer(text: str) -> bool:
    """True when ``text`` already ends with a footer produced by this module."""
    return bool(_FOOTER_RE.search(text or ""))


def strip_sources_footer(text: str) -> str:
    """Remove a previously appended footer (idempotency helper)."""
    return _FOOTER_RE.sub("", text or "").rstrip()


def _github_url(item: ExportItemDraft) -> str:
    if item.source_url:
        return item.source_url
    repo = (item.source_id or "").strip("/")
    return f"https://github.com/{repo}" if repo else ""


def _display_name(item: ExportItemDraft) -> str:
    name = (item.source_name or item.filename or item.source_id or "").strip()
    # Legacy Confluence captures carried a stray closing bracket from the
    # "[Source: Confluence: Title]" line.
    return name.rstrip("]").strip() or "Untitled"


def _escape_md(text: str) -> str:
    return text.replace("[", "\\[").replace("]", "\\]")


def _line(item: ExportItemDraft) -> str:
    label = _SOURCE_LABELS.get(item.source_type, item.source_type.title())
    name = _escape_md(_display_name(item))
    url = _github_url(item) if item.source_type == "GITHUB" else (item.source_url or "")
    if url:
        return f"- **{label}:** [{name}]({url})"
    return f"- **{label}:** {name}"


def render_sources_footer(items: Iterable[ExportItemDraft]) -> str:
    """Markdown "Sources" section for the documents that backed the answer.

    Only ``exportable`` items are listed (search hits the answer did not use are
    excluded), each document once, ordered by source system then retrieval rank.
    Returns ``""`` when there is nothing to cite.
    """
    used = [item for item in items if item.exportable and item.source_type in _SOURCE_LABELS]
    if not used:
        return ""
    seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    for source_type in _SOURCE_ORDER:
        group = sorted(
            (item for item in used if item.source_type == source_type),
            key=lambda item: (item.retrieval_rank if item.retrieval_rank is not None else 1_000_000),
        )
        for item in group:
            key = (item.source_type, item.source_id or _display_name(item))
            if key in seen:
                continue
            seen.add(key)
            lines.append(_line(item))
    if not lines:
        return ""
    return "\n\n" + _RULE + "\n" + _SOURCES_HEADING + "\n" + "\n".join(lines) + "\n"


def append_sources_footer(answer: str, items: Iterable[ExportItemDraft]) -> str:
    """Return ``answer`` with the Sources footer appended (replacing any old one)."""
    footer = render_sources_footer(items)
    base = strip_sources_footer(answer or "")
    if not footer:
        return base
    return base + footer
