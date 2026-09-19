"""Source Planner - deterministic knowledge-source selection before retrieval.

The agent historically relied on the model to decide which plugin to call inside
the Semantic Kernel function-calling loop. That works for a single source but is
unreliable for multi-source questions: the model would sometimes search one
source, produce a partial answer and then ask whether it should also search
another source. This module makes source selection an explicit, validated step
that runs *before* the agent synthesizes anything.

``LLMSourcePlanner`` asks the configured chat model for a compact JSON plan
(``{"sources": [{"type": ..., "reason": ...}]}``) and validates every proposed
type against the plugins that are actually registered for the turn, so the model
can never invent a tool or ignore the deployment's allowlists. Explicit user
requests ("Search GitHub", "Search Confluence and GitHub") short-circuit the
model call and are honored verbatim. When the model output is missing or
unparseable, a conservative keyword heuristic decides.

The planner is source-agnostic: adding a new knowledge source only requires
extending :data:`SOURCE_TYPES`, :data:`SOURCE_DESCRIPTIONS` and
:data:`PLUGIN_NAMES`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)

# Canonical source identifiers, in the order they are offered to the model.
SOURCE_TYPES: tuple[str, ...] = ("knowledge", "confluence", "sharepoint", "github")

# Human-readable capabilities, copied into the planner prompt. Keep each line
# short and specific so the model can map a question to the right source.
SOURCE_DESCRIPTIONS: dict[str, str] = {
    "knowledge": (
        "documents uploaded by the user in the current chat session "
        "(PDF, DOCX, PPTX, XLSX, TXT and other attached files)"
    ),
    "confluence": (
        "Confluence wiki pages: application and project documentation, architecture "
        "and system design pages, APIs, deployment and release guides, security, "
        "ADRs, runbooks and project docs"
    ),
    "sharepoint": (
        "SharePoint document libraries (PDF, Word, Excel, PowerPoint files): "
        "architecture and system design documents, CI/CD and deployment pipeline "
        "flows, technical designs, onboarding, policies, procedures and other "
        "enterprise documents. Holds documents that are NOT in Confluence, so it "
        "must be searched alongside Confluence for any architecture, design, "
        "deployment, pipeline or technical-documentation question"
    ),
    "github": (
        "GitHub repositories configured for this deployment: source code, READMEs, "
        "files, functions, APIs as implemented, configuration and dependencies"
    ),
}

# Maps a canonical source to the plugin name it is registered under.
PLUGIN_NAMES: dict[str, str] = {
    "knowledge": "Knowledge",
    "confluence": "Confluence",
    "sharepoint": "SharePoint",
    "github": "GitHub",
}

PLANNER_SYSTEM_INSTRUCTIONS = (
    "You are the SOURCE PLANNER for a knowledge assistant. You do not answer the "
    "user. You decide which knowledge sources must be searched before the final "
    "answer is written. Return only compact JSON."
)

_EXPLICIT_ACTIONS = (
    "search",
    "look",
    "check",
    "find",
    "query",
    "retrieve",
    "scan",
    "read",
    "pull",
    "use",
)

# Aliases that count as an explicit reference to a source when the user also uses
# an action verb nearby (for example "search github", "look in the repo").
_SOURCE_ALIASES: dict[str, tuple[str, ...]] = {
    "knowledge": (
        "uploaded document",
        "uploaded file",
        "uploaded pdf",
        "attached document",
        "attached file",
        "my document",
        "my file",
        "knowledge base",
        "this document",
    ),
    "confluence": ("confluence", "wiki"),
    "sharepoint": ("sharepoint",),
    "github": ("github", "repository", "repo", "codebase", "source code"),
}

# Keyword signals used only when the model's plan is unavailable or invalid.
_HEURISTIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "knowledge": (
        "uploaded",
        "attached",
        "my document",
        "my file",
        "this document",
        "this file",
        "knowledge base",
        "pdf",
        "docx",
    ),
    "confluence": (
        "confluence",
        "wiki",
        "architecture",
        "design document",
        "adr",
        "architecture decision",
        "runbook",
        "deployment guide",
        "api documentation",
        "technical documentation",
        "specification",
    ),
    "sharepoint": (
        "sharepoint",
        "onboarding",
        "policy",
        "policies",
        "procedure",
        "enterprise document",
        "internal document",
        "operational",
        # Documentation topics live in BOTH Confluence and SharePoint libraries.
        "architecture",
        "system design",
        "design document",
        "deployment",
        "pipeline",
        "ci/cd",
        "cicd",
        "ci cd",
        "workflow",
        "technical documentation",
        "specification",
    ),
    "github": (
        "github",
        "repository",
        "repo",
        "source code",
        "codebase",
        "function",
        "class",
        "implemented",
        "implementation",
        "controller",
        "service class",
        "config file",
        "pull request",
        "commit",
    ),
}

_ACTION_RE = re.compile(
    r"\b(?:search(?:es|ed)?|look(?:ing)?(?:\s+(?:in|at|into|through))?|check|find|"
    r"query|retrieve|scan|read|pull|use)\b"
)


@dataclass(frozen=True)
class SourceSelection:
    """A single selected source and why the planner chose it."""

    type: str
    reason: str = ""


class SourcePlanner(Protocol):
    """Selects the knowledge sources to retrieve before answering."""

    def plan(self, user_message: str, available: Sequence[str]) -> list[SourceSelection]:
        ...


class NoOpSourcePlanner:
    """Planner that selects nothing (keeps the raw agent loop unchanged)."""

    def plan(self, user_message: str, available: Sequence[str]) -> list[SourceSelection]:
        return []


def plugin_name_for(source: str) -> str:
    return PLUGIN_NAMES.get(source, source)


def source_for_plugin_name(name: str) -> str | None:
    for source, plugin_name in PLUGIN_NAMES.items():
        if plugin_name == name:
            return source
    return None


def detect_explicit_sources(user_message: str, available: Sequence[str]) -> list[str]:
    """Return sources the user explicitly asked to search, in canonical order.

    A source is explicit when one of its aliases appears close to an action verb
    ("search", "look in", "check", ...). This makes "Search GitHub" and "Search
    Confluence and GitHub" deterministic without a model round-trip.
    """
    text = (user_message or "").casefold()
    if not text:
        return []
    found: list[str] = []
    for source in SOURCE_TYPES:
        if source not in available:
            continue
        if _mentions_source_with_action(text, _SOURCE_ALIASES.get(source, ())):
            found.append(source)
    return found


def _mentions_source_with_action(text: str, aliases: Sequence[str]) -> bool:
    for alias in aliases:
        for match in re.finditer(re.escape(alias), text):
            start = max(0, match.start() - 40)
            end = min(len(text), match.end() + 40)
            if _ACTION_RE.search(text, start, end):
                return True
    return False


def heuristic_sources(user_message: str, available: Sequence[str]) -> list[str]:
    """Conservative keyword fallback used when the model plan is unusable."""
    text = (user_message or "").casefold()
    found: list[str] = []
    for source in SOURCE_TYPES:
        if source not in available:
            continue
        keywords = _HEURISTIC_KEYWORDS.get(source, ())
        if any(keyword in text for keyword in keywords):
            found.append(source)
    if found:
        return found
    # Nothing matched: default to the uploaded-document source when present,
    # otherwise the first available source, rather than searching everything.
    if "knowledge" in available:
        return ["knowledge"]
    return [available[0]] if available else []


# Phrases that widen a question beyond the source it names explicitly, e.g.
# "...from the attached document AND ALSO the Job Portal architecture across our
# connected knowledge sources".
_BREADTH_MARKERS: tuple[str, ...] = (
    "and also",
    "also retrieve",
    "also find",
    "also search",
    "also check",
    "also look",
    "as well as",
    "along with",
    "in addition",
    "additionally",
    "across our",
    "across all",
    "across the",
    "all connected",
    "connected knowledge",
    "all knowledge sources",
    "knowledge sources",
    "every source",
    "all sources",
    "other sources",
)

# Topic words that only a documentation/code source can answer; when they appear
# together with an explicit "uploaded document" reference, the question spans
# more than the upload.
_NON_UPLOAD_TOPICS: tuple[str, ...] = (
    "architecture",
    "system design",
    "workflow",
    "analysis report",
    "repository",
    "repo",
    "source code",
    "codebase",
    "implementation",
    "confluence",
    "sharepoint",
    "github",
)


def _asks_beyond_explicit_sources(user_message: str, explicit: Sequence[str]) -> bool:
    """True when the question also asks for knowledge outside its explicit source(s)."""
    text = " ".join((user_message or "").casefold().split())
    if any(marker in text for marker in _BREADTH_MARKERS):
        return True
    # "knowledge" (an uploaded file) named explicitly, but the question is also
    # about architecture / workflow / a repository -> other sources are needed.
    if list(explicit) == ["knowledge"] and any(topic in text for topic in _NON_UPLOAD_TOPICS):
        return True
    return False


def _ensure_named_repository_source(
    user_message: str, selections: list[SourceSelection], available: Sequence[str]
) -> list[SourceSelection]:
    """Add ``github`` when the question names an application in a way the router
    can map to a configured repository, but the plan omitted GitHub.

    The router itself decides *which* repository (it has the allowlist); the
    planner only needs to make sure GitHub is consulted at all.
    """
    if "github" not in available or any(s.type == "github" for s in selections):
        return selections
    text = (user_message or "").casefold()
    if any(term in text for term in ("architecture", "workflow", "implementation", "analysis report", "source code", "repository", "repo")):
        selections.append(
            SourceSelection(
                "github",
                "question asks about an application's architecture/workflow; the "
                "configured repository (if any) is consulted",
            )
        )
    return selections


# Documentation sources that hold *different* documents and must be searched
# together: a design or pipeline document may live in either system.
_DOCUMENTATION_SOURCES: tuple[str, ...] = ("confluence", "sharepoint")


def pair_documentation_sources(
    selections: list[SourceSelection], available: Sequence[str]
) -> list[SourceSelection]:
    """If the plan includes one documentation source, include the other too.

    SharePoint libraries and Confluence hold different documents (e.g. CI/CD
    pipeline PDFs only exist in SharePoint). A model plan that picks just one of
    them would silently miss the other's evidence, so the sibling is appended
    with an explanatory reason. Plans that select neither are left untouched.
    """
    chosen = {selection.type for selection in selections}
    if not chosen & set(_DOCUMENTATION_SOURCES):
        return selections
    result = list(selections)
    for source in _DOCUMENTATION_SOURCES:
        if source in available and source not in chosen:
            result.append(
                SourceSelection(
                    source,
                    "documentation may live in either Confluence or SharePoint; "
                    "searched alongside the planned documentation source",
                )
            )
    return result


def parse_source_plan(raw: str, available: Sequence[str]) -> list[str] | None:
    """Parse and validate a planner JSON response.

    Returns ``None`` when no usable plan was produced (the caller may fall back),
    or a (possibly empty) list of canonical source types when the model returned a
    well-formed ``sources`` list. An explicit empty list is a valid decision to
    search nothing.
    """
    data = _extract_json(raw)
    if not isinstance(data, dict):
        return None
    entries = data.get("sources")
    if not isinstance(entries, list):
        return None
    ordered: list[str] = []
    for entry in entries:
        source: str | None = None
        if isinstance(entry, dict):
            source = str(entry.get("type") or "").strip().casefold()
        elif isinstance(entry, str):
            source = entry.strip().casefold()
        if source is not None and source in available and source not in ordered:
            ordered.append(source)
    return ordered


def _extract_json(raw: str) -> object | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except ValueError:
        return None


class LLMSourcePlanner:
    """Source planner backed by a chat model, with deterministic overrides.

    ``complete`` is a synchronous callable that performs a single plain-text LLM
    completion (no tool calling). Keeping it injectable makes the planner easy to
    unit-test without a network call.
    """

    def __init__(self, complete: Callable[[str], str]) -> None:
        self._complete = complete

    def plan(self, user_message: str, available: Sequence[str]) -> list[SourceSelection]:
        available = tuple(dict.fromkeys(available))
        if not available:
            return []
        explicit = detect_explicit_sources(user_message, available)
        selections: list[SourceSelection] = [
            SourceSelection(source, "explicit user request") for source in explicit
        ]
        if explicit and not _asks_beyond_explicit_sources(user_message, explicit):
            # A pure "Search GitHub for X" style request: honor it verbatim.
            logger.info("Source planner honored explicit request: %s", explicit)
            return selections

        # Either nothing explicit, or the question ALSO asks about things the
        # explicit source cannot answer (e.g. "retrieve the CI/CD flow of the
        # attached document AND the Job Portal architecture across our connected
        # knowledge sources"). The explicit sources are kept and the model plans
        # the rest - an explicit mention must never *suppress* other sources.
        raw = ""
        try:
            raw = self._complete(self.build_prompt(user_message, available)) or ""
        except Exception:  # noqa: BLE001 - a planner failure must not break the turn
            logger.exception("Source planner model call failed; using keyword fallback")

        planned = parse_source_plan(raw, available)
        if planned is not None:
            chosen = {s.type for s in selections}
            selections.extend(
                SourceSelection(source, "selected by the source planner")
                for source in planned
                if source not in chosen
            )
            selections = pair_documentation_sources(selections, available)
            if explicit:
                selections = _ensure_named_repository_source(user_message, selections, available)
            logger.info("Source planner selected %s", [s.type for s in selections])
            return selections

        fallback = heuristic_sources(user_message, available)
        chosen = {s.type for s in selections}
        selections.extend(
            SourceSelection(source, "keyword fallback") for source in fallback if source not in chosen
        )
        logger.info("Source planner keyword fallback selected %s", [s.type for s in selections])
        return selections

    def build_prompt(self, user_message: str, available: Sequence[str]) -> str:
        lines = [
            PLANNER_SYSTEM_INSTRUCTIONS,
            "",
            "AVAILABLE SOURCES (choose only from these, by exact type name):",
        ]
        for source in available:
            lines.append(f"- {source}: {SOURCE_DESCRIPTIONS.get(source, source)}")
        lines.extend(
            [
                "",
                "RULES:",
                "- Choose every source that could contain relevant evidence; choose more",
                "  than one when the question spans documentation and code.",
                "- Confluence and SharePoint are BOTH documentation sources and hold",
                "  different documents. For any question about architecture, system",
                "  design, deployment, CI/CD pipelines, workflows or technical",
                "  documentation, select BOTH confluence and sharepoint when both are",
                "  available - never assume documentation is only in one of them.",
                "- Choose only the sources that are genuinely relevant; do not select all",
                "  sources by default, and do not always select only one.",
                "- Never invent a source type that is not in the list above.",
                "- Output only JSON in this exact shape:",
                '  {"sources": [{"type": "<type>", "reason": "<short reason>"}]}',
                "- If no source is relevant, output {\"sources\": []}.",
                "",
                f"USER QUESTION: {user_message}",
            ]
        )
        return "\n".join(lines)
