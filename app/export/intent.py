"""Export intent - format resolution and validation.

A "deterministic proposal" is always computed first from the *actual retrieved
sources* (content structure, native availability, source type). When enabled, an
LLM may recommend a different intent (a strict JSON ``{format, type, reason}``);
``ExportService`` then validates that recommendation against the retrieved
sources and the live set of available exporters. An unvalidatable or unsafe
recommendation - unknown format, unsupported scenario, extension abuse, mismatch
with the retrieved sources - is discarded in favour of the deterministic
proposal. The hard rule is that a single native artifact is always exported in
its original format; no LLM recommendation can override that.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Set as AbstractSet
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.export.errors import ExportNotFoundError, ExportValidationError
from app.export.formats import ScenarioType
from app.export.sources import ResolvedSource

logger = logging.getLogger(__name__)

# Formats a generated *document* (Confluence page / uploaded doc fallback) may use.
DOCUMENT_FORMATS = ("pdf", "docx", "md", "html", "json", "txt", "csv", "xlsx")
# Formats a generated *report* (GitHub analysis) may use.
REPORT_FORMATS = ("pdf", "docx", "md", "html", "json", "txt")
# Formats allowable for LLM recommendations (closed allowlist; never arbitrary).
RECOMMENDABLE_FORMATS = frozenset(DOCUMENT_FORMATS + REPORT_FORMATS)


class ExportIntent(BaseModel):
    """A validated export intent (``format``/``type``/``reason``)."""

    format: str = Field(min_length=1, max_length=16)
    type: str = Field(min_length=1, max_length=32)
    reason: str = ""

    @model_validator(mode="after")
    def _validate_fields(self) -> ExportIntent:
        if self.format not in {member.value for member in ScenarioType} and (
            self.format not in {
                "pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "json", "html", "zip",
            }
        ):
            raise ExportValidationError(
                f"Export format '{self.format}' is not a supported format."
            )
        if self.type not in {member.value for member in ScenarioType}:
            raise ExportValidationError(
                f"Export scenario '{self.type}' is not a supported scenario."
            )
        return self

    @classmethod
    def parse_llm_intent(cls, raw: object) -> ExportIntent | None:
        """Parse a strict ``{format,type,reason}`` JSON blob from the LLM."""
        if not isinstance(raw, str):
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        if not all(key in data for key in ("format", "type")):
            return None
        if not all(isinstance(data.get(key), str) for key in ("format", "type", "reason")):
            return None
        if str(data["format"]).lower() not in RECOMMENDABLE_FORMATS:
            return None
        try:
            return cls(
                format=str(data["format"]).lower(),
                type=str(data["type"]).upper(),
                reason=str(data.get("reason") or "")[:500],
            )
        except ExportValidationError:
            return None


def _pick(available: AbstractSet[str], preferred_order: list[str]) -> str:
    for candidate in preferred_order:
        if candidate in available:
            return candidate
    ordered = sorted(available)
    if ordered:
        return ordered[0]
    raise ExportValidationError("No generated export formats are available.")


def propose_intent(
    resolved: list[ResolvedSource], available: AbstractSet[str]
) -> ExportIntent:
    """Deterministic, content-driven baseline intent (used as validation target)."""
    if not resolved:
        raise ExportNotFoundError(
            "No retrievable sources were recorded for this chat message."
        )
    if len(resolved) == 1:
        source = resolved[0]
        if source.has_native_bytes and source.native_format:
            return ExportIntent(
                format=source.native_format,
                type=ScenarioType.NATIVE_FILE.value,
                reason=(
                    f"Single native source artifact '{source.source_name}' exported "
                    "in its original format."
                ),
            )
        if source.source_type == "GITHUB":
            return ExportIntent(
                format=_pick(available, ["pdf", "docx", "md", "html", "json", "txt"]),
                type=ScenarioType.GENERATED_REPORT.value,
                reason="GitHub repository functionality and architecture analysis.",
            )
        return ExportIntent(
            format=_generated_document_format(source, available),
            type=ScenarioType.GENERATED_DOCUMENT.value,
            reason=_generated_document_reason(source),
        )
    return ExportIntent(
        format="zip",
        type=ScenarioType.MULTI_ARTIFACT.value,
        reason=(
            f"Multiple heterogeneous source documents ({len(resolved)} artifacts) "
            "packaged as a ZIP while preserving each artifact's format."
        ),
    )


def _generated_document_format(source: ResolvedSource, available: AbstractSet[str]) -> str:
    kind = (source.structure or {}).get("kind") or "prose"
    if kind == "tabular":
        return _pick(available, ["csv", "xlsx"])
    if kind == "json":
        return "json"
    if kind == "html":
        return "html"
    if kind == "markdown":
        return "md"
    return _pick(available, ["docx", "pdf", "md", "txt"])


def _generated_document_reason(source: ResolvedSource) -> str:
    kind = (source.structure or {}).get("kind") or "prose"
    if source.source_type == "CONFLUENCE":
        base = f"Confluence page '{source.source_name}' generated into a document "
    elif source.source_type == "UPLOADED_DOCUMENT":
        base = f"Uploaded document '{source.source_name}' original bytes unavailable; generated "
    else:
        base = f"SharePoint document '{source.source_name}' generated "
    return base + f"(detected content structure: {kind})."


def validate_intent(
    intent: ExportIntent,
    resolved: list[ResolvedSource],
    available: AbstractSet[str],
) -> ExportIntent:
    """Validate ``intent`` against the retrieved sources and exporters.

    Raises :class:`ExportValidationError` on any mismatch; the caller falls back
    to the deterministic proposal.
    """
    if not resolved:
        raise ExportNotFoundError(
            "No retrievable sources were recorded for this chat message."
        )
    fmt = intent.format.lower()
    if fmt == "zip":
        if intent.type == ScenarioType.MULTI_ARTIFACT.value and len(resolved) >= 2:
            return intent
        raise ExportValidationError(
            "ZIP export requires at least two sources (MULTI_ARTIFACT)."
        )
    if intent.type == ScenarioType.MULTI_ARTIFACT.value:
        if fmt == "zip" and len(resolved) >= 2:
            return intent
        raise ExportValidationError(
            "MULTI_ARTIFACT export must use the 'zip' format with 2+ sources."
        )
    if fmt == "native":
        raise ExportValidationError("A generated export cannot use format 'native'.")
    if fmt not in available:
        raise ExportValidationError(
            f"Format '{fmt}' has no available exporter in this deployment."
        )

    # Exactly one native artifact: the original format is preserved, always.
    if len(resolved) == 1 and resolved[0].has_native_bytes:
        native_format = resolved[0].native_format or fmt
        return ExportIntent(
            format=native_format,
            type=ScenarioType.NATIVE_FILE.value,
            reason="Single native source artifact exported in its original format.",
        )

    if len(resolved) == 1:
        source = resolved[0]
        if intent.type == ScenarioType.GENERATED_REPORT.value:
            if source.source_type != "GITHUB":
                raise ExportValidationError(
                    "GENERATED_REPORT is only valid for a single GitHub repository."
                )
            if fmt not in REPORT_FORMATS:
                raise ExportValidationError(
                    f"Format '{fmt}' is not a valid report format."
                )
            return intent
        if intent.type == ScenarioType.GENERATED_DOCUMENT.value:
            if source.source_type == "GITHUB":
                raise ExportValidationError(
                    "A GitHub repository must be exported as a report (GENERATED_REPORT)."
                )
            if fmt not in DOCUMENT_FORMATS:
                raise ExportValidationError(
                    f"Format '{fmt}' is not a valid generated document format."
                )
            return intent
        raise ExportValidationError(
            f"Scenario '{intent.type}' does not match the single retrieved source."
        )

    raise ExportValidationError(
        "Multiple sources require a ZIP (MULTI_ARTIFACT) export."
    )


class IntentRecommender:
    """Optional LLM-assisted export intent recommendation (strict JSON).

    The recommendation is advisory only: :func:`validate_intent` still enforces
    every safety/source rule and falls back to the deterministic proposal on any
    violation.
    """

    def __init__(self, mistral_service: Any | None = None, *, enabled: bool = True) -> None:
        self._mistral_service = mistral_service
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled and self._mistral_service is not None

    def recommend(
        self,
        resolved: list[ResolvedSource],
        available: AbstractSet[str],
        user_hint: str = "",
    ) -> ExportIntent | None:
        if not self.enabled or not resolved:
            return None
        summary_lines = [
            f"- {source.source_type} '{source.source_name}' "
            f"(mime={source.mime_type or 'n/a'}, native={'yes' if source.has_native_bytes else 'no'}, "
            f"structure={(source.structure or {}).get('kind') or 'n/a'})"
            for source in resolved
        ]
        single_native = len(resolved) == 1 and resolved[0].has_native_bytes
        instruction = (
            "You propose an export format for retrieved knowledge. "
            "Respond with ONLY strict JSON: {\"format\": \"...\", \"type\": \"...\", \"reason\": \"...\"}.\n"
            f"Allowed formats: {', '.join(sorted(available))}.\n"
            f"Scenarios: {', '.join(item.value for item in ScenarioType)}.\n"
        )
        if single_native:
            instruction += "Hard rule: a single native artifact is exported in its ORIGINAL format (NATIVE_FILE); do not propose anything else.\n"
        prompt = (
            instruction
            + "Retrieved sources:\n"
            + "\n".join(summary_lines)
            + "\n"
            + (f"User request: {user_hint}\n" if user_hint else "")
            + "Return the JSON now."
        )
        try:
            raw = self._mistral_service.generate_response(prompt)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - never break the export on an LLM failure
            logger.warning("Export intent recommendation failed: %s", exc)
            return None
        return ExportIntent.parse_llm_intent(raw)
