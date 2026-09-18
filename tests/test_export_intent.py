"""Tests for export intent proposal + validation (`app.export.intent`)."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.export.errors import ExportNotFoundError, ExportValidationError
from app.export.formats import ScenarioType
from app.export.intent import (
    DOCUMENT_FORMATS,
    REPORT_FORMATS,
    ExportIntent,
    IntentRecommender,
    propose_intent,
    validate_intent,
)
from app.export.sources import ResolvedSource

AVAILABLE = frozenset(DOCUMENT_FORMATS + REPORT_FORMATS)


def _source(
    source_type: str,
    source_name: str,
    *,
    native: bytes | None = None,
    native_format: str | None = None,
    structure: dict | None = None,
) -> ResolvedSource:
    return ResolvedSource(
        item=None,
        source_type=source_type,
        source_id=source_name,
        source_name=source_name,
        filename=source_name,
        content="the source content",
        mime_type="text/plain",
        native_bytes=native,
        native_format=native_format,
        structure=structure or {"kind": "prose"},
    )


# --- propose_intent ---------------------------------------------------------


def test_propose_single_native_upload_keeps_original_format() -> None:
    source = _source("UPLOADED_DOCUMENT", "policy.pdf", native=b"%PDF-1.4", native_format="pdf")
    intent = propose_intent([source], AVAILABLE)
    assert intent.format == "pdf"
    assert intent.type == ScenarioType.NATIVE_FILE.value


def test_propose_single_github_is_generated_report() -> None:
    source = _source("GITHUB", "acme/api")
    intent = propose_intent([source], AVAILABLE)
    assert intent.type == ScenarioType.GENERATED_REPORT.value
    assert intent.format in REPORT_FORMATS


def test_propose_single_upload_without_bytes_is_generated_document() -> None:
    source = _source("UPLOADED_DOCUMENT", "notes.txt")
    intent = propose_intent([source], AVAILABLE)
    assert intent.type == ScenarioType.GENERATED_DOCUMENT.value
    assert intent.format == "docx"


def test_propose_tabular_structure_prefers_spreadsheet() -> None:
    source = _source("CONFLUENCE", "rates", structure={"kind": "tabular"})
    assert propose_intent([source], AVAILABLE).format in {"csv", "xlsx"}
    assert propose_intent([source], {"json", "txt"}).format == "json"


def test_propose_json_and_html_and_markdown_structures() -> None:
    assert propose_intent([_source("CONFLUENCE", "api", structure={"kind": "json"})], AVAILABLE).format == "json"
    assert propose_intent([_source("CONFLUENCE", "page", structure={"kind": "html"})], AVAILABLE).format == "html"
    assert propose_intent([_source("CONFLUENCE", "doc", structure={"kind": "markdown"})], AVAILABLE).format == "md"


def test_propose_multiple_sources_is_zip_multiple_artifacts() -> None:
    sources = [
        _source("UPLOADED_DOCUMENT", "one.pdf", native=b"%PDF", native_format="pdf"),
        _source("CONFLUENCE", "two"),
    ]
    intent = propose_intent(sources, AVAILABLE)
    assert intent.format == "zip"
    assert intent.type == ScenarioType.MULTI_ARTIFACT.value


def test_propose_no_sources_raises_not_found() -> None:
    with pytest.raises(ExportNotFoundError):
        propose_intent([], AVAILABLE)


# --- validate_intent --------------------------------------------------------


def test_validate_enforces_native_hard_rule() -> None:
    source = _source("UPLOADED_DOCUMENT", "policy.pdf", native=b"%PDF", native_format="pdf")
    intent = ExportIntent(format="docx", type=ScenarioType.GENERATED_DOCUMENT.value)
    validated = validate_intent(intent, [source], AVAILABLE)
    assert validated.format == "pdf"
    assert validated.type == ScenarioType.NATIVE_FILE.value


def test_validate_rejects_zip_for_single_source() -> None:
    source = _source("CONFLUENCE", "page")
    intent = ExportIntent(format="zip", type=ScenarioType.MULTI_ARTIFACT.value)
    with pytest.raises(ExportValidationError):
        validate_intent(intent, [source], AVAILABLE)


def test_validate_confuses_multiple_artifact_scenario() -> None:
    source = _source("CONFLUENCE", "page")
    intent = ExportIntent(format="pdf", type=ScenarioType.MULTI_ARTIFACT.value)
    with pytest.raises(ExportValidationError):
        validate_intent(intent, [source], AVAILABLE)


def test_validate_rejects_native_format_for_generated() -> None:
    with pytest.raises(ExportValidationError):
        ExportIntent(format="native", type=ScenarioType.GENERATED_DOCUMENT.value)


def test_validate_rejects_format_without_exporter() -> None:
    source = _source("CONFLUENCE", "page")
    intent = ExportIntent(format="pdf", type=ScenarioType.GENERATED_DOCUMENT.value)
    with pytest.raises(ExportValidationError):
        validate_intent(intent, [source], frozenset({"txt"}))


def test_validate_reports_only_for_github() -> None:
    source = _source("CONFLUENCE", "page")
    intent = ExportIntent(format="pdf", type=ScenarioType.GENERATED_REPORT.value)
    with pytest.raises(ExportValidationError):
        validate_intent(intent, [source], AVAILABLE)


def test_validate_github_must_be_report() -> None:
    source = _source("GITHUB", "acme/api")
    intent = ExportIntent(format="docx", type=ScenarioType.GENERATED_DOCUMENT.value)
    with pytest.raises(ExportValidationError):
        validate_intent(intent, [source], AVAILABLE)


def test_validate_accepts_legit_report_and_document() -> None:
    github = _source("GITHUB", "acme/api")
    report = ExportIntent(format="md", type=ScenarioType.GENERATED_REPORT.value)
    assert validate_intent(report, [github], AVAILABLE).format == "md"

    confluence = _source("CONFLUENCE", "page")
    document = ExportIntent(format="docx", type=ScenarioType.GENERATED_DOCUMENT.value)
    assert validate_intent(document, [confluence], AVAILABLE).format == "docx"


def test_validate_zip_with_two_sources_passes() -> None:
    sources = [_source("CONFLUENCE", "a"), _source("SHAREPOINT", "b")]
    intent = ExportIntent(format="zip", type=ScenarioType.MULTI_ARTIFACT.value)
    assert validate_intent(intent, sources, AVAILABLE) is intent


def test_validate_empty_sources_raises_not_found() -> None:
    with pytest.raises(ExportNotFoundError):
        validate_intent(ExportIntent(format="pdf", type=ScenarioType.GENERATED_DOCUMENT.value), [], AVAILABLE)


# --- ExportIntent.parse_llm_intent ------------------------------------------


def test_parse_llm_intent_valid_json() -> None:
    intent = ExportIntent.parse_llm_intent('{"format": "pdf", "type": "generated_report", "reason": "report"}')
    assert intent is not None
    assert intent.format == "pdf"
    assert intent.type == "GENERATED_REPORT"


def test_parse_llm_intent_rejects_garbage() -> None:
    assert ExportIntent.parse_llm_intent("not json") is None
    assert ExportIntent.parse_llm_intent('[1, 2, 3]') is None
    assert ExportIntent.parse_llm_intent('{"format": "pdf"}') is None  # missing type
    assert ExportIntent.parse_llm_intent('{"format": 3, "type": "x"}') is None
    assert ExportIntent.parse_llm_intent(None) is None


def test_parse_llm_intent_rejects_disallowed_format() -> None:
    assert ExportIntent.parse_llm_intent('{"format": "exe", "type": "generated_report"}') is None
    assert ExportIntent.parse_llm_intent('{"format": "zip", "type": "multi_artifact"}') is None  # zip not recommendable


def test_parse_llm_intent_rejects_bad_scenario() -> None:
    assert ExportIntent.parse_llm_intent('{"format": "pdf", "type": "not_a_scenario"}') is None


# --- IntentRecommender ------------------------------------------------------


def test_recommender_disabled_returns_none() -> None:
    recommender = IntentRecommender(enabled=False)
    assert recommender.recommend([_source("CONFLUENCE", "page")], AVAILABLE) is None


def test_recommender_without_mistral_returns_none() -> None:
    recommender = IntentRecommender(None)
    assert not recommender.enabled
    assert recommender.recommend([_source("CONFLUENCE", "page")], AVAILABLE) is None


def test_recommender_parses_valid_response() -> None:
    mistral = Mock()
    mistral.generate_response.return_value = '{"format": "txt", "type": "generated_document", "reason": "easy"}'
    recommender = IntentRecommender(mistral, enabled=True)
    intent = recommender.recommend([_source("CONFLUENCE", "page")], AVAILABLE, user_hint="plain text")
    assert intent is not None
    assert intent.format == "txt"
    assert "plain text" in mistral.generate_response.call_args.args[0]


def test_recommender_returns_none_on_malformed_or_error() -> None:
    mistral = Mock()
    mistral.generate_response.side_effect = RuntimeError("llm down")
    recommender = IntentRecommender(mistral, enabled=True)
    assert recommender.recommend([_source("CONFLUENCE", "page")], AVAILABLE) is None


def test_recommender_builds_single_native_hard_rule_instruction() -> None:
    mistral = Mock()
    mistral.generate_response.return_value = '{"format": "docx", "type": "generated_document"}'
    recommender = IntentRecommender(mistral, enabled=True)
    recommender.recommend([_source("UPLOADED_DOCUMENT", "policy.pdf", native=b"%PDF", native_format="pdf")], AVAILABLE)
    prompt = mistral.generate_response.call_args.args[0]
    assert "ORIGINAL format (NATIVE_FILE)" in prompt
