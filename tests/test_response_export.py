"""Unit tests for the lightweight AI response TXT export builder."""

from __future__ import annotations

from datetime import datetime

from app.export.response_export import (
    ResponseExportSource,
    render_response_txt,
    response_export_filename,
)


def test_render_includes_query_response_and_timestamp() -> None:
    timestamp = datetime(2026, 9, 15, 10, 30, 0)
    text = render_response_txt(
        user_query="Explain the architecture",
        assistant_response="The system is layered.",
        timestamp=timestamp,
    )

    assert "AI Response Export" in text
    assert "Explain the architecture" in text
    assert "The system is layered." in text
    assert "2026-09-15 10:30:00" in text


def test_render_includes_exported_at_when_provided() -> None:
    exported = datetime(2026, 9, 15, 11, 0, 0)
    text = render_response_txt(
        user_query="q",
        assistant_response="a",
        exported_at=exported,
    )
    assert "Exported: 2026-09-15 11:00:00" in text


def test_render_omits_sources_when_empty() -> None:
    text = render_response_txt(user_query="q", assistant_response="a")
    assert "Sources:" not in text
    # No trailing empty-blank-section artefacts.
    assert not text.endswith("Sources:")


def test_render_includes_sources_with_url_and_id() -> None:
    sources = (
        ResponseExportSource(
            source_type="CONFLUENCE",
            source_name="Policy Page",
            source_id="1234",
            source_url="https://confluence.example/pages/1234",
        ),
        ResponseExportSource(
            source_type="UPLOADED_DOCUMENT",
            source_name="policy.pdf",
            source_id="policy.pdf",
        ),
    )
    text = render_response_txt(user_query="q", assistant_response="a", sources=sources)

    assert "Sources:" in text
    assert "- CONFLUENCE: Policy Page (https://confluence.example/pages/1234)" in text
    assert "- UPLOADED_DOCUMENT: policy.pdf" in text


def test_render_falls_back_when_source_name_missing() -> None:
    sources = (ResponseExportSource(source_type="GITHUB", source_name=""),)
    text = render_response_txt(user_query="q", assistant_response="a", sources=sources)
    assert "GITHUB" in text


def test_render_uses_placeholder_for_empty_parts() -> None:
    text = render_response_txt(user_query="", assistant_response="")
    assert "  " not in text
    assert text.count("\n-\n") >= 2  # both the query and the response show '-'
    assert "User Query:" in text
    assert "AI Response:" in text


def test_response_export_filename_sanitized() -> None:
    assert response_export_filename(123) == "ai-response-123.txt"
    assert response_export_filename(-7) == "ai-response--7.txt"