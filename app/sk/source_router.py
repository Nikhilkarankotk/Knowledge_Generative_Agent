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
        if matched:
            # Every repository the question names is retrieved (a question may
            # span several applications), bounded to avoid runaway retrieval.
            selected = matched[: max(self._github_max_repositories, 3)]
            logger.info("GitHub pre-retrieval matched repositories %s", selected)
        elif len(repositories) == 1:
            # A single configured repository is unambiguous.
            selected = repositories
        elif repositories:
            # Several repositories and none is named in the question: do NOT
            # guess (the first alphabetical repo would be exported as if it were
            # relevant). The agent can still call GitHub tools itself if the
            # answer needs code evidence.
            selected = []
            logger.info(
                "GitHub pre-retrieval: none of the %d configured repositories is "
                "named in the question; skipping repository retrieval",
                len(repositories),
            )
        else:
            selected = []
        parts = [listing]
        for repo in selected:
            try:
                parts.append(str(plugin.retrieve_repository_contents(repo) or ""))
            except Exception:  # noqa: BLE001
                logger.exception("Pre-retrieval failed for GitHub repository %s", repo)
        return "\n\n".join(part for part in parts if part)


_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _parse_allowed_repositories(text: str) -> list[str]:
    """Extract ``owner/name`` repositories from a ``list_allowed_repositories`` result.

    Accepts both formats the plugin has produced: one ``[Source: GitHub: owner/name]``
    attribution line per repository, and the current listing of ``- owner/name``
    bullets under a single ``[Source: GitHub]`` header.
    """
    repositories: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip().split(":", 1)[0].strip()
        if candidate and "#" not in candidate and _REPO_RE.match(candidate):
            if candidate not in repositories:
                repositories.append(candidate)

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("[Source: GitHub:"):
            add(stripped[len("[Source: GitHub:") :].split("]", 1)[0])
        elif stripped.startswith(("- ", "* ", "• ")):
            add(stripped[2:])
    return repositories


def _repo_tokens(value: str) -> list[str]:
    """Lower-cased word tokens of a repo name or question (``Job_Portal-Web`` ->
    ``job portal web``)."""
    return [t for t in re.split(r"[^a-z0-9]+", (value or "").casefold()) if t]


def _repository_matches(repo: str, query: str) -> bool:
    """Best-effort match of a repository against the user's question.

    A repository matches when its name appears verbatim, or when every word of
    its name (split on ``_``/``-``/case) appears in the question in order - so
    ``Job_Portal_Web_Application`` matches "job portal web application" and
    ``n8n`` matches "the CICD architecture of n8n", while generic single-word
    repositories such as ``express`` must appear as a whole word.
    """
    name = repo.rsplit("/", 1)[-1]
    if not name:
        return False
    # Verbatim repo name as a whole word ("...of n8n from...", "Job_Portal_Web_Application").
    if re.search(rf"(?<![a-z0-9]){re.escape(name.casefold())}(?![a-z0-9])", (query or "").casefold()):
        return True
    words = _repo_tokens(name)
    query_words = _repo_tokens(query)
    if not words:
        return False
    if len(words) == 1:
        return words[0] in query_words
    # All name words present, in order (allowing gaps), e.g. "job portal web
    # application". The words must appear as a contiguous phrase modulo generic
    # filler, otherwise "E-commerce_Application" would match any question that
    # mentions an "e"... and an "application" somewhere.
    joined_query = " ".join(query_words)
    joined_name = " ".join(words)
    if joined_name in joined_query:
        return True
    # Allow the generic suffix words to be omitted from the question:
    # "job portal" alone matches Job_Portal_Web_Application.
    generic = {"application", "app", "web", "service", "project", "repo", "repository"}
    core = [w for w in words if w not in generic]
    return len(core) >= 1 and " ".join(core) in joined_query and any(len(w) > 2 for w in core)


_TEMPLATE_OPEN = re.compile(r"\{\{")
_TEMPLATE_CLOSE = re.compile(r"\}\}")


def neutralize_template_syntax(text: str) -> str:
    """Make retrieved content safe to embed in the agent's instructions.

    The instructions are rendered by Semantic Kernel's prompt template engine,
    which treats ``{{ ... }}`` as a function call. Real repository content
    routinely contains that syntax (GitHub Actions ``${{ inputs.node-version }}``,
    Handlebars/Jinja templates, Helm charts...) and would make the whole turn
    fail with "Failed to tokenize code block". Insert a zero-width space between
    the braces so the text reads the same to the model but is no longer a token.
    """
    if not text or "{{" not in text and "}}" not in text:
        return text
    text = _TEMPLATE_OPEN.sub("{\u200b{", text)
    return _TEMPLATE_CLOSE.sub("}\u200b}", text)


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
        lines.append(neutralize_template_syntax((text or "(no evidence returned)").strip()))
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
