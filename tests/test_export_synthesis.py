"""Tests for :mod:`app.export.synthesis` (LLM narrative synthesis for exports)."""

from __future__ import annotations

from types import SimpleNamespace

from app.export.export_service import ExportService
from app.export.registry import ExportPayload, ExportSection
from app.export.synthesis import (
    ReportSynthesizer,
    build_evidence_text,
    build_synthesis_prompt,
    parse_sections,
)

SETTINGS = SimpleNamespace(export_include_summary=True)


class StubLLM:
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.requests: list[str] = []

    def generate_response(self, prompt: str) -> str:
        self.requests.append(prompt)
        return self.response


class RaiseLLM(StubLLM):
    def generate_response(self, prompt: str) -> str:
        self.requests.append(prompt)
        raise RuntimeError("boom")


def _payload() -> ExportPayload:
    return ExportPayload(
        title="acme/payments",
        source_url="https://github.com/acme/payments",
        content="The payment service handles charges.",
        sections=[
            ExportSection(
                heading="Dependencies & Technology Stack",
                body="- Java 17 (spring boot)\n- Postgres\n- Redis",
                evidence="Observed from source",
            )
        ],
    )


def test_synthesizer_disabled_returns_empty() -> None:
    synthesizer = ReportSynthesizer(StubLLM("## Technology Stack\n- Java"), enabled=False)
    assert synthesizer.synthesize(_payload(), kind="report") == []


def test_synthesizer_without_llm_returns_empty() -> None:
    synthesizer = ReportSynthesizer(None, enabled=True)
    assert synthesizer.enabled is False
    assert synthesizer.synthesize(_payload(), kind="report") == []


def test_synthesizer_parses_structured_report_sections() -> None:
    raw = (
        "## Exact Functionality\n"
        "Charges are created via ChargeService.charge().\n"
        "## Code Flow\n"
        "main.py -> routes -> ChargeService -> LedgerRepository.\n"
        "## Technology Stack\n"
        "- Java\n- Postgres\n"
        "## Other\n"
        "ignored\n"
        "## Key Components\n"
        "ChargeService, LedgerRepository."
    )
    synthesizer = ReportSynthesizer(StubLLM(raw), enabled=True)
    sections = synthesizer.synthesize(_payload(), kind="report")

    assert [section.heading for section in sections] == [
        "Exact Functionality",
        "Code Flow",
        "Technology Stack",
        "Key Components",
    ]
    assert sections[0].body == "Charges are created via ChargeService.charge()."
    assert sections[0].evidence == "Generated analysis (LLM)"
    assert sections[2].body.startswith("- Java")


def test_synthesizer_accepts_document_kind_headings() -> None:
    raw = "## Executive Summary\nPolicy overview.\n## Key Insights\n- monthly receipts"
    synthesizer = ReportSynthesizer(StubLLM(raw), enabled=True)
    sections = synthesizer.synthesize(_payload(), kind="document")
    assert [section.heading for section in sections] == ["Executive Summary", "Key Insights"]


def test_synthesizer_llm_failure_returns_empty() -> None:
    synthesizer = ReportSynthesizer(RaiseLLM(), enabled=True)
    assert synthesizer.synthesize(_payload(), kind="report") == []


def test_synthesizer_unstructured_output_returns_empty() -> None:
    synthesizer = ReportSynthesizer(StubLLM("just some prose"), enabled=True)
    assert synthesizer.synthesize(_payload(), kind="report") == []


def test_synthesizer_short_prose_without_headings_returns_empty() -> None:
    synthesizer = ReportSynthesizer(StubLLM("Test assistant response"), enabled=True)
    assert synthesizer.synthesize(_payload(), kind="report") == []


def test_evidence_text_is_bounded_and_contains_sections() -> None:
    payload = _payload()
    text = build_evidence_text(payload, max_chars=1000)
    assert "acme/payments" in text
    assert "Technology Stack" in text
    assert "- Redis" in text

    truncated = build_evidence_text(payload, max_chars=10)
    assert truncated.endswith("[Evidence truncated to fit the synthesis context.]")


def test_evidence_text_orders_code_sections_first() -> None:
    payload = ExportPayload(
        sections=[
            ExportSection(heading="Repository Information", body="meta", evidence="Observed from source"),
            ExportSection(heading="File: src/app.py", body="def main():\n    run()", evidence="Observed from source"),
            ExportSection(heading="Functions & Classes", body="fn list", evidence="Observed from source"),
        ]
    )
    text = build_evidence_text(payload, max_chars=10_000)
    assert text.index("src/app.py") < text.index("Repository Information")
    assert text.index("src/app.py") < text.index("Functions & Classes")


def test_prompt_mentions_kind_and_grounding_rules() -> None:
    report_prompt = build_synthesis_prompt("evidence", kind="report")
    assert "Architecture Design" in report_prompt
    assert "Technology Stack" in report_prompt
    assert "Exact Functionality" in report_prompt
    assert "Code Flow" in report_prompt
    assert "do not invent" in report_prompt.lower()

    document_prompt = build_synthesis_prompt("evidence", kind="document")
    assert "Key Insights" in document_prompt


def test_parse_sections_ignores_unknown_headings() -> None:
    raw = "## Bogus\nStuff\n## Key Insights\nReal"
    sections = parse_sections(raw, headings=("Key Insights", "Executive Summary"))
    assert [section.heading for section in sections] == ["Key Insights"]
    assert sections[0].body == "Real"


def test_prepend_synthesis_orders_narrative_first() -> None:
    payload = ExportPayload(sections=[ExportSection(heading="Evidence", body="raw", evidence="Observed")])
    synthesizer_obj = SimpleNamespace(
        synthesize=lambda target, kind: [
            ExportSection(heading="Executive Summary", body="Narrative.", evidence="Generated analysis (LLM)")
        ]
    )
    service = ExportService(
        export_repo=object(),  # type: ignore[arg-type]
        settings=SETTINGS,
        synthesizer=synthesizer_obj,  # type: ignore[arg-type]
        limits=SimpleNamespace(),
    )
    service._prepend_synthesis(payload, kind="report")
    assert [section.heading for section in payload.sections] == ["Executive Summary", "Evidence"]


def test_prepend_synthesis_noop_without_synthesizer() -> None:
    payload = ExportPayload(sections=[ExportSection(heading="Evidence", body="raw")])
    service = ExportService(export_repo=object(), settings=SETTINGS, limits=SimpleNamespace())  # type: ignore[arg-type]
    service._prepend_synthesis(payload, kind="report")
    assert [section.heading for section in payload.sections] == ["Evidence"]


def test_synthesizer_truncates_oversized_section_bodies() -> None:
    raw = "## Executive Summary\n" + ("wordy " * 50_000)
    synthesizer = ReportSynthesizer(StubLLM(raw), enabled=True)
    sections = synthesizer.synthesize(_payload(), kind="report")
    assert sections
    assert len(sections[0].body) <= 40_001
