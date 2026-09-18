"""Tests for retrieval capture (parsers + the RetrievalCapture accumulator)."""

from __future__ import annotations

from app.export.capture import (
    ExportItemDraft,
    RetrievalCapture,
    parse_confluence_page,
    parse_confluence_search,
    parse_github_code_search,
    parse_github_target,
    parse_rag_context,
    parse_sharepoint_items,
)


def test_parse_rag_context_splits_by_source_marker() -> None:
    text = (
        "[Source: a.pdf]\nfirst chunk\nsecond line\n"
        "\n[Source: b.pdf]\nthird chunk\n"
        "[Source: a.pdf]\nfourth chunk\n"
    )
    assert parse_rag_context(text) == [
        ("a.pdf", "first chunk\nsecond line"),
        ("b.pdf", "third chunk"),
        ("a.pdf", "fourth chunk"),
    ]


def test_parse_rag_context_ignores_unknown_markers() -> None:
    assert parse_rag_context("no source markers here") == []
    assert parse_rag_context("") == []


def test_parse_confluence_search_blocks() -> None:
    text = (
        "[Source: Confluence: Rates (space: FIN)]\n"
        "Page id: 1234\n"
        "URL: https://wiki/rates\n"
        "Excerpt: interest rates are monthly\n"
        "\n"
        "[Source: Confluence: Onboarding (space: HR)]\n"
        "Page id: 99\n"
        "URL: https://wiki/onboarding\n"
        "Excerpt: welcome kit\n"
    )
    pages = parse_confluence_search(text)
    assert len(pages) == 2
    assert pages[0]["page_id"] == "1234"
    assert pages[0]["title"] == "Rates"
    assert pages[0]["space"] == "FIN"
    assert pages[0]["url"] == "https://wiki/rates"
    assert pages[1]["page_id"] == "99"


def test_parse_confluence_page_content() -> None:
    text = (
        "[Source: Confluence: Expenses (space: FIN)]\n"
        "https://wiki/expenses\n"
        "Expense policy section one\n"
        "Expense policy section two\n"
    )
    parsed = parse_confluence_page(text)
    assert parsed is not None
    assert parsed["title"] == "Expenses"
    assert parsed["url"] == "https://wiki/expenses"
    assert "section one" in parsed["content"]


def test_parse_confluence_page_unknown_marker_returns_none() -> None:
    assert parse_confluence_page("no confluence source here") is None


def test_parse_github_target_variants() -> None:
    assert parse_github_target("[Source: GitHub: acme/api]") == {"repo": "acme/api"}
    assert parse_github_target("[Source: GitHub: acme/api:src/main.py]") == {
        "repo": "acme/api",
        "path": "src/main.py",
    }
    assert parse_github_target("[Source: GitHub: acme/api#42]") == {
        "repo": "acme/api",
        "issue": "42",
    }
    assert parse_github_target("nothing here") is None


def test_parse_sharepoint_items() -> None:
    text = (
        "[Source: SharePoint: policy.pdf]\n"
        "URL: https://share/policy\n"
        "Drive id: d1\n"
        "Document id: 7\n"
        "Size: 1234\n"
        "MimeType: application/pdf\n"
        "Modified: 2026-01-02\n"
        "the policy says reset annually\n"
        "\n"
        "[Source: SharePoint: reporting.xlsx]\n"
        "Document id: 8\n"
        "URL: https://share/reporting\n"
    )
    items = parse_sharepoint_items(text)
    assert len(items) == 2
    first = items[0]
    assert first["name"] == "policy.pdf"
    assert first["document_id"] == "7"
    assert first["drive_id"] == "d1"
    assert first["size"] == 1234
    assert first["mime_type"] == "application/pdf"
    assert "reset annually" in first["content"]


def test_parse_github_code_search() -> None:
    text = (
        "[Source: GitHub: acme/api:src/payments.py]\n"
        "URL: https://github.com/acme/api/blob/main/src/payments.py\n"
        "\n"
        "[Source: GitHub: acme/web:src/app.py]\n"
        "URL: https://github.com/acme/web\n"
    )
    results = parse_github_code_search(text)
    assert len(results) == 2
    assert results[0]["repo"] == "acme/api"
    assert results[0]["path"] == "src/payments.py"
    assert results[1]["url"] == "https://github.com/acme/web"


def test_capture_deduplicates_and_merges_content() -> None:
    capture = RetrievalCapture()
    first = ExportItemDraft(
        source_type="UPLOADED_DOCUMENT",
        source_id="report.pdf",
        source_name="report.pdf",
        filename="report.pdf",
        metadata={"a": 1},
    )
    first.merge_content("alpha")
    capture.add(first)

    second = ExportItemDraft(
        source_type="UPLOADED_DOCUMENT",
        source_id="report.pdf",
        source_name="report.pdf",
        filename="report.pdf",
        metadata={"b": 2},
    )
    second.merge_content("beta")
    capture.add(second)

    items = capture.items
    assert len(items) == 1
    merged = items[0]
    assert "alpha" in (merged.content_reference or "")
    assert "beta" in (merged.content_reference or "")
    assert merged.metadata == {"a": 1, "b": 2}


def test_capture_assigns_rank_in_insertion_order() -> None:
    capture = RetrievalCapture()
    capture.add(ExportItemDraft(source_type="CONFLUENCE", source_id="1"))
    capture.add(ExportItemDraft(source_type="GITHUB", source_id="acme/api"))
    capture.add(ExportItemDraft(source_type="SHAREPOINT", source_id="doc1"))
    assert [item.retrieval_rank for item in capture.items] == [0, 1, 2]


def test_capture_distinguishes_types_for_same_id() -> None:
    capture = RetrievalCapture()
    capture.add(ExportItemDraft(source_type="UPLOADED_DOCUMENT", source_id="doc.pdf"))
    capture.add(ExportItemDraft(source_type="CONFLUENCE", source_id="doc.pdf"))
    assert len(capture.items) == 2
