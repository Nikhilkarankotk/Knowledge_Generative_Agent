"""Resolve subject-matter experts (SMEs) from a chat turn's retrieved sources.

The dashboard "Top Experts" card is backed by this service. Experts are derived
*only* from the person metadata actually captured for the current query
(``export_context`` for the session's latest assistant turn): a source owner or
creator, the most recent editor, and named contributors. The resolver never
invents identities: a source without person metadata contributes no candidate,
and when nothing is available the result is simply an empty list.

Ranking follows the product priority:

1. ``Owner`` - the owner/creator of a relevant Confluence/SharePoint document.
2. ``Recent Editor`` - the person who most recently modified relevant content.
3. ``Contributor`` - another relevant person (e.g. the owner of another
   document, or a person associated with several relevant sources).

People are deduplicated by name across sources, associated with every relevant
source they touched, and ordered by role priority, then by how many relevant
sources they are associated with, then by the most recent edit timestamp (so
editors of the most recently updated pages rank higher).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.repositories import ExportContextRepository

logger = logging.getLogger(__name__)

ROLE_OWNER = "Owner"
ROLE_RECENT_EDITOR = "Recent Editor"
ROLE_CONTRIBUTOR = "Contributor"

_ROLE_PRIORITY = {ROLE_OWNER: 0, ROLE_RECENT_EDITOR: 1, ROLE_CONTRIBUTOR: 2}
_ROLE_VERB = {
    ROLE_OWNER: "Owner of",
    ROLE_RECENT_EDITOR: "Most recent editor of",
    ROLE_CONTRIBUTOR: "Contributor to",
}

_SOURCE_LABELS = {
    "CONFLUENCE": "Confluence",
    "SHAREPOINT": "SharePoint",
    "GITHUB": "GitHub",
    "UPLOADED_DOCUMENT": "uploaded document",
}
_DEFAULT_SOURCE_LABEL = "knowledge"

_MAX_EXPERTS = 3


@dataclass
class Expert:
    name: str
    role: str
    source: str
    source_count: int
    reason: str
    roles: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def initials(self) -> str:
        return _initials(self.name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "roles": list(self.roles),
            "source": self.source,
            "sourceCount": self.source_count,
            "reason": self.reason,
            "initials": self.initials,
            "sources": list(self.sources),
        }


@dataclass
class _Candidate:
    name: str
    roles: set[str] = field(default_factory=set)
    source_types: set[str] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)
    role_source_type: str = ""
    recency: float = 0.0

    def best_role(self) -> str:
        return min(self.roles, key=lambda role: _ROLE_PRIORITY.get(role, 99))

    def add(
        self, role: str, source_type: str, source_name: str, when: str = ""
    ) -> None:
        if role not in self.roles:
            self.roles.add(role)
        if source_type and source_type not in self.source_types:
            self.source_types.add(source_type)
        if source_name and source_name not in self.sources:
            self.sources.append(source_name)
        # Remember which source type granted the highest-priority role so the
        # card can show the most relevant origin for this person.
        current = self.role_source_type
        if not current or _ROLE_PRIORITY.get(role, 99) <= _ROLE_PRIORITY.get(
            self.best_role(), 99
        ):
            self.role_source_type = source_type or current
        value = _recency(when)
        if value > self.recency:
            self.recency = value


def _metadata(item: Any) -> dict[str, Any]:
    meta = getattr(item, "meta", None)
    return meta if isinstance(meta, dict) else {}


def _first_str(meta: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _normalize(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def _recency(when: str) -> float:
    """Epoch seconds for an ISO-8601 timestamp; 0.0 when missing/unparseable."""
    text = (when or "").strip()
    if not text:
        return 0.0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0


def _initials(name: str) -> str:
    parts = [part for part in re.split(r"[\s._\-@]+", name or "") if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _record(
    candidates: dict[str, _Candidate],
    name: str,
    role: str,
    source_type: str,
    source_name: str,
    when: str = "",
) -> None:
    clean = (name or "").strip()
    if not clean:
        return
    key = _normalize(clean)
    candidate = candidates.get(key)
    if candidate is None:
        candidate = _Candidate(name=clean)
        candidates[key] = candidate
    candidate.add(role, source_type, source_name, when)


def _people(meta: dict[str, Any], *keys: str) -> list[str]:
    """Collect non-empty string people from possibly-list metadata values."""
    people: list[str] = []
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str):
            clean = value.strip()
            if clean:
                people.append(clean)
        elif isinstance(value, (list, tuple, set)):
            for entry in value:
                if isinstance(entry, str) and entry.strip():
                    people.append(entry.strip())
    return people


def _recent_editors(meta: dict[str, Any], fallback_when: str) -> list[tuple[str, str]]:
    """Recent editors as ``(name, when)`` pairs, most recent version first.

    Prefers ``recent_editor_details`` (per-editor version timestamps) and
    supplements with names from ``recent_editors`` that lack structured details.
    Names missing a timestamp fall back to the page-level ``fallback_when``.
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    details = meta.get("recent_editor_details")
    if isinstance(details, (list, tuple)):
        for detail in details:
            if not isinstance(detail, dict):
                continue
            name = str(detail.get("name") or "").strip()
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            when = str(detail.get("when") or "").strip() or fallback_when
            pairs.append((name, when))
    for name in _people(meta, "recent_editors"):
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        pairs.append((name, fallback_when))
    return pairs


def resolve_experts(
    items: Iterable[Any], *, max_experts: int = _MAX_EXPERTS
) -> list[Expert]:
    """Rank the people associated with the retrieved sources (top ``max_experts``)."""
    candidates: dict[str, _Candidate] = {}
    for item in items or []:
        source_type = str(getattr(item, "source_type", "") or "").upper()
        source_name = str(
            getattr(item, "source_name", "")
            or getattr(item, "source_id", "")
            or ""
        ).strip()
        source_id = str(getattr(item, "source_id", "") or "").strip()
        meta = _metadata(item)
        when = _first_str(
            meta, "last_modified", "modified", "last_modified_date", "updated"
        )

        owner = _first_str(meta, "owner", "creator", "created_by", "author")
        editor = _first_str(
            meta, "last_editor", "last_modified_by", "editor", "modified_by"
        )
        _record(candidates, owner, ROLE_OWNER, source_type, source_name, when)
        _record(candidates, editor, ROLE_RECENT_EDITOR, source_type, source_name, when)

        # Recent editors come from the Confluence version history (ordered most
        # recent first) and from explicit contributor lists on other sources.
        # Per-editor version timestamps, when available, rank fresher editors
        # above people whose last edit is older.
        for recent_name, recent_when in _recent_editors(meta, when):
            _record(
                candidates,
                recent_name,
                ROLE_RECENT_EDITOR,
                source_type,
                source_name,
                recent_when,
            )
        for contributor in _people(meta, "contributors"):
            _record(
                candidates,
                contributor,
                ROLE_CONTRIBUTOR,
                source_type,
                source_name,
                when,
            )

        # A GitHub repository carries its owner in the source id ("owner/name").
        # That is real, attributable metadata from the retrieved source.
        if source_type == "GITHUB" and "/" in source_id:
            repo_owner = source_id.split("/", 1)[0].strip()
            _record(
                candidates,
                repo_owner,
                ROLE_CONTRIBUTOR,
                "GITHUB",
                source_name or source_id,
            )

    ranked = sorted(
        candidates.values(),
        key=lambda candidate: (
            _ROLE_PRIORITY.get(candidate.best_role(), 99),
            -len(candidate.sources),
            -candidate.recency,
            candidate.name.casefold(),
        ),
    )
    return [_to_expert(candidate) for candidate in ranked[:max_experts]]


def _to_expert(candidate: _Candidate) -> Expert:
    role = candidate.best_role()
    source = _SOURCE_LABELS.get(
        candidate.role_source_type, _DEFAULT_SOURCE_LABEL
    )
    roles = sorted(candidate.roles, key=lambda item: _ROLE_PRIORITY.get(item, 99))
    return Expert(
        name=candidate.name,
        role=role,
        source=source,
        source_count=len(candidate.sources),
        reason=_reason(candidate, role, source),
        roles=roles,
        sources=list(candidate.sources),
    )


def _reason(candidate: _Candidate, role: str, source: str) -> str:
    verb = _ROLE_VERB.get(role, "Associated with")
    count = len(candidate.sources)
    if count <= 1:
        name = candidate.sources[0] if candidate.sources else "source"
        return f"{verb} the relevant {source} source '{name}'"
    return f"{verb} {count} relevant {source} sources"


class SmeExpertService:
    """Resolve the dashboard experts for a session's current query."""

    def __init__(self, export_repo: ExportContextRepository) -> None:
        self._export_repo = export_repo

    def resolve(
        self, session_id: str, chat_message_id: int | None = None
    ) -> dict[str, Any]:
        context = self._load_context(session_id, chat_message_id)
        if context is None:
            return {"chatMessageId": None, "experts": []}
        items = self._export_repo.find_items(context.id)
        return {
            "chatMessageId": context.chat_message_id,
            "experts": [expert.to_dict() for expert in resolve_experts(items)],
        }

    def _load_context(
        self, session_id: str, chat_message_id: int | None
    ) -> Any | None:
        if chat_message_id is not None:
            context = self._export_repo.find_by_chat_message_id(chat_message_id)
            if context is None or context.session_id != session_id:
                return None
            return context
        contexts = self._export_repo.find_by_session_id(session_id)
        return contexts[-1] if contexts else None
