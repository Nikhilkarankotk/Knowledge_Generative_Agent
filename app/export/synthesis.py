"""LLM-assisted narrative synthesis for generated exports.

Generated reports (GitHub) and documents (Confluence, SharePoint, uploads) are
rendered deterministically from :class:`~app.export.registry.ExportPayload`
sections. :class:`ReportSynthesizer` optionally asks the LLM to turn the raw
evidence into a readable narrative (functionality flow, architecture, technology
stack for repositories; an elaborated overview for documents).

Design rules:

* The synthesis is advisory. The deterministic evidence sections always remain in
  the payload - the narrative is *prepended* as lead-in sections.
* The LLM is instructed to base everything strictly on the provided evidence and
  to mark anything not observed as "Unavailable"; hallucinated content is not used.
* Any failure (timeout, transport error, non-structured output) yields no sections
  and never breaks the export (identical to how intent recommendations degrade).
"""

from __future__ import annotations

import logging
from typing import Any

from app.export.registry import ExportPayload, ExportSection

logger = logging.getLogger(__name__)

_MAX_EVIDENCE_CHARS = 50_000
_MAX_SECTION_BODY_CHARS = 40_000

_REPORT_HEADINGS = (
    "Executive Summary",
    "Exact Functionality",
    "Code Flow",
    "Architecture Design",
    "Technology Stack",
    "Key Components",
)

_DOCUMENT_HEADINGS = (
    "Executive Summary",
    "Key Insights",
)

# Headings used for the sampled-source appendix (``File: <path>`` sections) so the
# LLM evidence can be ordered code-first regardless of renderer order.
_CODE_SECTION_PREFIX = "File: "
_CODE_EXTENSIONS = (
    ".py",
    ".java",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".rs",
    ".kt",
    ".cs",
    ".rb",
    ".php",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".vue",
)


class ReportSynthesizer:
    """Optional LLM narrative writer for generated exports (never mandatory)."""

    def __init__(
        self,
        mistral_service: Any | None = None,
        *,
        enabled: bool = True,
        max_evidence_chars: int = _MAX_EVIDENCE_CHARS,
    ) -> None:
        self._mistral_service = mistral_service
        self._enabled = enabled
        self._max_evidence_chars = max_evidence_chars

    @property
    def enabled(self) -> bool:
        return self._enabled and self._mistral_service is not None

    def synthesize(
        self,
        payload: ExportPayload,
        *,
        kind: str,
    ) -> list[ExportSection]:
        """Return narrative sections for ``payload`` (``kind`` = ``report``|``document``).

        Returns an empty list when disabled, on LLM failure, or when the model
        output is not structured enough to be trusted.
        """
        if not self.enabled or payload is None:
            return []
        evidence = build_evidence_text(payload, max_chars=self._max_evidence_chars)
        if not evidence.strip():
            return []
        prompt = build_synthesis_prompt(evidence, kind=kind)
        try:
            raw = self._mistral_service.generate_response(prompt)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - never break an export on an LLM failure
            logger.warning("Export narrative synthesis failed: %s", exc)
            return []
        sections = parse_sections(
            raw,
            headings=_REPORT_HEADINGS if kind == "report" else _DOCUMENT_HEADINGS,
        )
        if not sections and raw and len(raw.strip()) > 200:
            sections = [
                ExportSection(
                    heading="Executive Summary",
                    body=raw.strip()[:_MAX_SECTION_BODY_CHARS],
                    evidence="Generated analysis (LLM)",
                )
            ]
        return sections


def build_evidence_text(payload: ExportPayload, *, max_chars: int) -> str:
    """Flatten a payload into a bounded, well-delimited evidence text.

    Sampled-source sections (``File: <path>``) are emitted before the analytical
    sections so the LLM narrative is grounded in the actual code first.
    """
    parts: list[str] = []
    if payload.title:
        parts.append(f"TITLE: {payload.title}")
    if payload.source_url:
        parts.append(f"SOURCE: {payload.source_url}")
    if payload.content:
        parts.append("FULL CONTENT:\n" + payload.content.strip())
    code_sections = [s for s in payload.sections if _is_code_section(s)]
    other_sections = [s for s in payload.sections if not _is_code_section(s)]
    for section in [*code_sections, *other_sections]:
        parts.append(
            f"### {section.heading}"
            + (f" [evidence: {section.evidence}]" if section.evidence else "")
            + "\n"
            + section.body.strip()
        )
    if payload.rows:
        parts.append(
            "TABLE:\n"
            + (("\t".join(payload.headers)) + "\n" if payload.headers else "")
            + "\n".join("\t".join(str(cell) for cell in row) for row in payload.rows)
        )
    text = "\n\n".join(parts).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + "\n\n[Evidence truncated to fit the synthesis context.]"
    return text


def _is_code_section(section: ExportSection) -> bool:
    heading = (section.heading or "").strip()
    if heading.lower().startswith(_CODE_SECTION_PREFIX.lower()):
        return True
    if "/" not in heading:
        return False
    lower = heading.lower()
    return any(lower.endswith(ext) for ext in _CODE_EXTENSIONS)


def build_synthesis_prompt(evidence: str, *, kind: str) -> str:
    """Build the LLM prompt that turns retrieval evidence into narrative sections."""
    if kind == "report":
        instruction = (
            "You are a software-architecture analyst producing a repository analysis report. "
            "You are given the README, repository structure and - most importantly - the actual "
            "sampled source code. Produce a precise, evidence-grounded analysis. Base EVERY "
            "statement strictly on the provided evidence: do NOT invent files, functions, "
            "features, frameworks, or behaviour. Where the code does not show something, write "
            "'Not observed in the exported evidence'.\n"
            "Write exactly these sections in order:\n"
            "- Executive Summary\n"
            "- Exact Functionality (a precise inventory of what this application actually does: "
            "each feature, command, route or exported function visible in the code, what triggers "
            "it and where it lives - name the real files/functions)\n"
            "- Code Flow (how control flows through the application: the entry point(s), startup, "
            "request/command lifecycle, the function call chains between the observed modules, and "
            "any persistence/API/UI interaction you can trace from the code - name the real "
            "functions and files)\n"
            "- Architecture Design (modules, layers, components and how they communicate)\n"
            "- Technology Stack (only languages, frameworks, libraries and tools actually listed "
            "in the code or dependency files)\n"
            "- Key Components (the most important classes/functions/files you observed and why)\n"
            "Use concise markdown (## section headings, short bullet lists, small tables where "
            "useful). Output the whole report as one message."
        )
    else:
        instruction = (
            "You are an enterprise-knowledge analyst. Produce an elaborated analysis of the "
            "document(s) below, clearly tied to what they state. Do not invent facts not "
            "present in the content; where the evidence is silent say 'Not stated in the "
            "source document(s)'.\n"
            "Write exactly these sections in order:\n"
            "- Executive Summary\n"
            "- Key Insights\n"
            "Use concise markdown (## section headings, short bullet lists). Output the "
            "whole analysis as one message."
        )
    return "\n\n".join([instruction, "=== EVIDENCE ===\n" + evidence, "Write the report now."])


def parse_sections(raw: str, *, headings: tuple[str, ...]) -> list[ExportSection]:
    """Parse ``## <heading>`` blocks from the LLM output into export sections.

    Returns an empty list when no recognisable headings are present (the caller
    then keeps the deterministic payload unchanged).
    """
    if not raw:
        return []
    lines = raw.splitlines()
    result: list[ExportSection] = []
    current: ExportSection | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("##"):
            title = stripped.lstrip("#").strip()
            if title.lower() in {heading.lower() for heading in headings}:
                if current is not None and current.body.strip():
                    current.body = current.body.strip()[:_MAX_SECTION_BODY_CHARS]
                    result.append(current)
                current = ExportSection(
                    heading=title,
                    body="",
                    evidence="Generated analysis (LLM)",
                )
                continue
            if current is not None and current.body.strip():
                current.body = current.body.strip()[:_MAX_SECTION_BODY_CHARS]
                result.append(current)
                current = None
            continue
        if current is not None:
            current.body += line + "\n"
    if current is not None and current.body.strip():
        current.body = current.body.strip()[:_MAX_SECTION_BODY_CHARS]
        result.append(current)
    return result
