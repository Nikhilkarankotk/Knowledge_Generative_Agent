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
    strip_retrieval_state,
)

_STATE_BLOCK = (
    "\n\n=== CONFLUENCE RETRIEVAL STATE (authoritative evidence summary; machine-readable) ===\n"
    "guidance text\n"
    '{"source": "confluence", "evidence_found": true}\n'
    "=== END CONFLUENCE RETRIEVAL STATE ==="
)


def _candidate(page_id: str, title: str) -> ExportItemDraft:
    return ExportItemDraft(
        source_type="CONFLUENCE", source_id=page_id, source_name=title, exportable=False
    )


def test_promote_relevant_candidates_picks_only_the_page_the_answer_is_about() -> None:
    """Real scenario: search returned six design pages, the agent answered about
    E-commerce without opening any page. Only the E-commerce page (plus the
    GitHub repo that was read) may be exported - not Netflix/Twitter/Amazon..."""
    capture = RetrievalCapture()
    for page_id, title in [
        ("2916353", "Amazon System Design and Architecture"),
        ("983203", "Explore Confluence Features"),
        ("622593", "Netflix System Design and Architecture"),
        ("524300", "Twitter System Design and Architecture"),
        ("3112961", "n8n System Design and Architecture"),
        ("2785284", "E-Commerce Application System Design and Architecture"),
    ]:
        capture.add(_candidate(page_id, title))
    capture.add(
        ExportItemDraft(
            source_type="GITHUB", source_id="acme/E-commerce_Application",
            source_name="acme/E-commerce_Application", exportable=True,
        )
    )

    promoted = capture.promote_relevant_candidates(
        "Tell me about the E-commerce architecture",
        "Here is the **E-Commerce System Design and Architectural Overview** based on ...\n\n"
        + "Lots of detail. " * 40
        # A passing comparison deep in the body must NOT pull Netflix in.
        + "Unlike Netflix, the catalogue is product-centric.",
    )

    assert promoted == ["2785284"]
    exportable = {i.source_id for i in capture.items if i.exportable}
    assert exportable == {"2785284", "acme/E-commerce_Application"}


def test_promote_relevant_candidates_is_a_noop_when_a_page_was_read() -> None:
    """If get_page was used, that page is the ground truth - never widen it."""
    capture = RetrievalCapture()
    capture.add(_candidate("1", "Netflix System Design and Architecture"))
    capture.add(_candidate("2", "Twitter System Design and Architecture"))
    read = _candidate("1", "Netflix System Design and Architecture")
    read.exportable = True
    capture.add(read)  # merge promotes page 1

    promoted = capture.promote_relevant_candidates("netflix and twitter", "Netflix ... Twitter ...")

    assert promoted == []
    assert {i.source_id for i in capture.items if i.exportable} == {"1"}


def test_promote_relevant_candidates_ignores_generic_title_words() -> None:
    capture = RetrievalCapture()
    capture.add(_candidate("9", "System Design and Architecture"))  # no distinctive terms
    assert capture.promote_relevant_candidates("system design", "system design answer") == []


def _sp_candidate(doc_id: str, filename: str) -> ExportItemDraft:
    return ExportItemDraft(
        source_type="SHAREPOINT", source_id=doc_id, source_name=filename,
        filename=filename, exportable=False,
    )


def test_promote_sharepoint_keeps_only_the_asked_applications_document() -> None:
    """Real library: three PDFs match "pipeline"; a question about the Job Portal
    must export ONLY the Job Portal PDF, not n8n's or Amazon's."""
    capture = RetrievalCapture()
    capture.add(_sp_candidate("JP", "Job_Portal_Web_Application CICD Pipeline flow.pdf"))
    capture.add(_sp_candidate("N8", "n8n CICD Pipeline flow.pdf"))
    capture.add(_sp_candidate("AZ", "System Design and Architecture of Amazon Shopping Kart.pdf"))

    promoted = capture.promote_relevant_candidates(
        "Explain the CI/CD pipeline flow for the Job Portal web application",
        "Here is the CI/CD pipeline flow for the Job Portal web application ...",
    )

    assert promoted == ["JP"]
    assert [i.source_name for i in capture.items if i.exportable] == [
        "Job_Portal_Web_Application CICD Pipeline flow.pdf"
    ]


def test_promote_sharepoint_matches_on_subject_when_doc_kind_words_differ() -> None:
    """"n8n" is the subject; the question says "deployment" not "pipeline"."""
    capture = RetrievalCapture()
    capture.add(_sp_candidate("N8", "n8n CICD Pipeline flow.pdf"))
    capture.add(_sp_candidate("JP", "Job_Portal_Web_Application CICD Pipeline flow.pdf"))
    assert capture.promote_relevant_candidates("How is n8n deployed?", "n8n is deployed via ...") == ["N8"]


def test_promote_sharepoint_noop_when_a_document_was_opened() -> None:
    capture = RetrievalCapture()
    capture.add(_sp_candidate("N8", "n8n CICD Pipeline flow.pdf"))
    opened = _sp_candidate("N8", "n8n CICD Pipeline flow.pdf")
    opened.exportable = True
    capture.add(opened)  # get_sharepoint_document read -> merge promotes
    capture.add(_sp_candidate("JP", "Job_Portal_Web_Application CICD Pipeline flow.pdf"))
    assert capture.promote_relevant_candidates("job portal pipeline", "job portal ...") == []
    assert [i.source_id for i in capture.items if i.exportable] == ["N8"]


def test_parse_sharepoint_items_skips_scoped_result_header() -> None:
    text = (
        "[Source: SharePoint: site host,438af,2512] - Search results for \"x\" "
        "(folder: Shared Documents, content of 1 match(es))\n\n"
        "[Source: SharePoint: Job_Portal_Web_Application CICD Pipeline flow.pdf]\n"
        "URL: https://sp/x.pdf\nDrive id: d1\nDocument id: i1\nSize: 10 bytes"
    )
    items = parse_sharepoint_items(text)
    assert [i["name"] for i in items] == ["Job_Portal_Web_Application CICD Pipeline flow.pdf"]


def test_strip_retrieval_state_removes_only_the_state_block() -> None:
    text = "[Source: Confluence: Netflix]\nPage id: 1\nURL: https://wiki/1" + _STATE_BLOCK
    stripped = strip_retrieval_state(text)
    assert "RETRIEVAL STATE" not in stripped
    assert "evidence_found" not in stripped
    assert "Page id: 1" in stripped


def test_confluence_parsers_ignore_retrieval_state_block() -> None:
    search = (
        "[Source: Confluence: Netflix System Design (space: ARCH)]\n"
        "Page id: 9000\n"
        "URL: https://wiki/9000\n"
        "Excerpt: design"
        + _STATE_BLOCK
    )
    (page,) = parse_confluence_search(search)
    assert page["page_id"] == "9000"
    assert page["title"] == "Netflix System Design"

    page_text = (
        "[Source: Confluence: Netflix System Design]\n"
        "URL: https://wiki/9000\n"
        "Design content"
        + _STATE_BLOCK
    )
    parsed = parse_confluence_page(page_text)
    assert parsed is not None
    assert parsed["content"] == "Design content"
    assert "RETRIEVAL STATE" not in parsed["content"]


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


def test_parse_confluence_search_captures_people() -> None:
    text = (
        "[Source: Confluence: Payments (space: PAY)]\n"
        "Page id: 5\n"
        "URL: https://wiki/payments\n"
        "Last modified by: Bob Ray\n"
        "Last modified: 2026-01-02\n"
        "Excerpt: payment flow\n"
    )
    (page,) = parse_confluence_search(text)
    assert page["last_editor"] == "Bob Ray"
    assert page["modified"] == "2026-01-02"


def test_parse_confluence_page_captures_owner_and_strips_header() -> None:
    text = (
        "[Source: Confluence: Expenses]\n"
        "Owner: Alice Doe\n"
        "Last modified by: Bob Ray\n"
        "Recent editor: Bob Ray | 2026-01-02T10:00:00Z | 3\n"
        "Recent editor: Alice Doe | 2026-01-01T10:00:00Z | 1\n"
        "https://wiki/expenses\n"
        "Expense policy section one\n"
        "Expense policy section two\n"
    )
    parsed = parse_confluence_page(text)
    assert parsed is not None
    assert parsed["owner"] == "Alice Doe"
    assert parsed["last_editor"] == "Bob Ray"
    assert parsed["recent_editors"] == ["Bob Ray", "Alice Doe"]
    assert parsed["recent_editor_details"] == [
        {"name": "Bob Ray", "when": "2026-01-02T10:00:00Z", "version": "3"},
        {"name": "Alice Doe", "when": "2026-01-01T10:00:00Z", "version": "1"},
    ]
    assert parsed["url"] == "https://wiki/expenses"
    assert parsed["content"].startswith("Expense policy section one")
    assert "Owner:" not in parsed["content"]
    assert "Recent editor:" not in parsed["content"]
    assert "https://wiki/expenses" not in parsed["content"]


def test_parse_confluence_search_captures_recent_editors() -> None:
    text = (
        "[Source: Confluence: Netflix System Design (space: ARCH)]\n"
        "Page id: 9000\n"
        "URL: https://wiki/9000\n"
        "Owner: Nikhil Karankot\n"
        "Last modified by: Tejaswinik\n"
        "Recent editor: Tejaswinik | 2026-02-02T10:00:00Z | 2\n"
        "Recent editor: Nikhil Karankot | 2026-01-01T10:00:00Z | 1\n"
        "Last modified: 2026-02-02T10:00:00Z\n"
        "Excerpt: design\n"
    )
    (page,) = parse_confluence_search(text)
    assert page["owner"] == "Nikhil Karankot"
    assert page["last_editor"] == "Tejaswinik"
    assert page["recent_editors"] == ["Tejaswinik", "Nikhil Karankot"]
    assert page["recent_editor_details"] == [
        {"name": "Tejaswinik", "when": "2026-02-02T10:00:00Z", "version": "2"},
        {"name": "Nikhil Karankot", "when": "2026-01-01T10:00:00Z", "version": "1"},
    ]
    assert page["modified"] == "2026-02-02T10:00:00Z"


def test_parse_sharepoint_items_captures_people() -> None:
    text = (
        "[Source: SharePoint: policy.pdf]\n"
        "URL: https://share/policy\n"
        "Document id: 7\n"
        "Created by: Alice Doe\n"
        "Modified by: Bob Ray\n"
        "MimeType: application/pdf\n"
        "the policy says reset annually\n"
    )
    (item,) = parse_sharepoint_items(text)
    assert item["owner"] == "Alice Doe"
    assert item["last_editor"] == "Bob Ray"
    assert "reset annually" in item["content"]


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
