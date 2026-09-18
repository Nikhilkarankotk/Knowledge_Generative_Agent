"""Source Router - deterministic pre-retrieval of the planner's selected sources.

The router runs *before* the agent writes its answer. It invokes each selected
source's own plugin (never a parallel retrieval stack), so every existing
guarantee is preserved: Confluence space validation, the GitHub
``GITHUB_ALLOWED_REPOSITORIES`` allowlist, SharePoint scope enforcement, RAG
session isolation, the shared per-source evidence ledger and export capture.

Each source is retrieved independently. A failure or empty result in one source
is recorded but never overwrites the evidence returned by the others; the merged
evidence block keeps every source's ``[Source: ...]`` attribution intact.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.sk.source_planner import SourceSelection

logger = logging.getLogger(__name__)

_SOURCE_LINE_RE = re.compile(r"\[Source:")


@dataclass
class PrefetchResult:
    """Merged pre-retrieval output for one turn."""

    evidence: list[tuple[str, str]] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    without_evidence: list[str] = field(default_factory=list)

    def text_for(self, source: str) -> str:
        for candidate, text in self.evidence:
            if candidate == source:
                return text
        return ""


class SourceRouter:
    """Invokes the selected sources' plugins before the final synthesis."""

    def __init__(self, *, github_max_repositories: int = 1) -> None:
        self._github_max_repositories = max(1, github_max_repositories)

    def prefetch(
        self,
        selections: list[SourceSelection],
        plugins_by_source: dict[str, Any],
        query: str,
    ) -> PrefetchResult:
        result = PrefetchResult()
        for selection in selections:
            plugin = plugins_by_source.get(selection.type)
            if plugin is None:
                continue
            text = self._retrieve(selection.type, plugin, query)
            result.evidence.append((selection.type, text))
            if _SOURCE_LINE_RE.search(text or ""):
                result.used.append(selection.type)
            else:
                result.without_evidence.append(selection.type)
        return result

    # -- per-source retrieval ---------------------------------------------------

    def _retrieve(self, source: str, plugin: Any, query: str) -> str:
        try:
            if source == "knowledge":
                return str(plugin.search_knowledge(query) or "")
            if source == "confluence":
                return str(plugin.search_pages(query) or "")
            if source == "sharepoint":
                return str(plugin.search_sharepoint_content(query) or "")
            if source == "github":
                return self._retrieve_github(plugin, query)
        except Exception:  # noqa: BLE001 - one source must never break the turn
            logger.exception("Pre-retrieval failed for source %s", source)
        return ""

    def _retrieve_github(self, plugin: Any, query: str) -> str:
        listing = str(plugin.list_allowed_repositories() or "")
        repositories = _parse_allowed_repositories(listing)
        matched = [repo for repo in repositories if _repository_matches(repo, query)]
        selected = matched or repositories[: self._github_max_repositories]
        parts = [listing]
        for repo in selected:
            try:
                parts.append(str(plugin.retrieve_repository_contents(repo) or ""))
            except Exception:  # noqa: BLE001
                logger.exception("Pre-retrieval failed for GitHub repository %s", repo)
        return "\n\n".join(part for part in parts if part)


def _parse_allowed_repositories(text: str) -> list[str]:
    """Extract ``owner/name`` repositories from a list_allowed_repositories result."""
    repositories: list[str] = []
    for line in (text or "").splitlines():
        if not line.startswith("[Source: GitHub:"):
            continue
        raw = line[len("[Source: GitHub:") :].split("]", 1)[0].strip()
        raw = raw.split(":", 1)[0].strip()
        if not raw or "#" in raw:
            continue
        if raw not in repositories:
            repositories.append(raw)
    return repositories


def _repository_matches(repo: str, query: str) -> bool:
    """Best-effort match of a repository name against the user's question."""
    name = repo.rsplit("/", 1)[-1].casefold()
    return bool(name) and name in (query or "").casefold()


def render_evidence(result: PrefetchResult, selections: list[SourceSelection]) -> str:
    """Render the merged evidence block appended to the agent's instructions."""
    if not selections:
        return ""
    reasons = {selection.type: selection.reason for selection in selections}
    lines = [
        "",
        "=== RETRIEVED EVIDENCE ===",
        "The source planner selected the sources below and they were searched "
        "before this answer. Treat this evidence as already retrieved this turn.",
    ]
    if result.used:
        lines.append("SOURCES USED: " + ", ".join(result.used))
    if result.without_evidence:
        lines.append(
            "SOURCES WITH NO EVIDENCE: "
            + ", ".join(result.without_evidence)
            + " (empty or failed; this must not suppress the other sources)"
        )
    for source in [selection.type for selection in selections]:
        reason = reasons.get(source)
        if reason:
            lines.append(f"Plan rationale for {source}: {reason}")
    for source, text in result.evidence:
        lines.append(f"--- {source.upper()} EVIDENCE ---")
        lines.append((text or "(no evidence returned)").strip())
    lines.extend(
        [
            "=== END RETRIEVED EVIDENCE ===",
            "",
            "Write ONE final answer that synthesizes all of the evidence above. "
            "Never ask the user for permission to search another source. Preserve "
            "every [Source: ...] attribution and end with a 'Sources used:' list "
            "naming only the sources whose evidence you actually used.",
        ]
    )
    return "\n".join(lines)
