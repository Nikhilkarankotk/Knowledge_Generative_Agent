"""Tests for the "Sources" footer appended to grounded AI responses."""

from __future__ import annotations

from app.export.capture import ExportItemDraft, RetrievalCapture
from app.export.sources_footer import (
    append_sources_footer,
    has_sources_footer,
    render_sources_footer,
    strip_sources_footer,
)


def _item(source_type: str, source_id: str, name: str, url: str | None = None, *, exportable=True, rank=0):
    return ExportItemDraft(
        source_type=source_type, source_id=source_id, source_name=name, source_url=url,
        exportable=exportable, retrieval_rank=rank,
    )


def test_footer_lists_each_used_document_with_a_link_grouped_by_source() -> None:
    items = [
        _item("CONFLUENCE", "622593", "Netflix System Design and Architecture]",
              "https://wiki/pages/622593", rank=2),
        _item("SHAREPOINT", "JP", "Job_Portal_Web_Application CICD Pipeline flow.pdf",
              "https://sp/jp.pdf", rank=3),
        _item("GITHUB", "acme/Job_Portal", "acme/Job_Portal", rank=1),  # url derived
        _item("UPLOADED_DOCUMENT", "Netflix CICD.pdf", "Netflix CICD.pdf", rank=0),  # no url
    ]
    footer = render_sources_footer(items)
    assert footer.startswith("\n\n---\n**Sources**\n")
    lines = [line for line in footer.splitlines() if line.startswith("- ")]
    assert lines == [
        "- **Uploaded document:** Netflix CICD.pdf",
        "- **Confluence:** [Netflix System Design and Architecture](https://wiki/pages/622593)",
        "- **SharePoint:** [Job_Portal_Web_Application CICD Pipeline flow.pdf](https://sp/jp.pdf)",
        "- **GitHub:** [acme/Job_Portal](https://github.com/acme/Job_Portal)",
    ]


def test_footer_excludes_search_hits_the_answer_did_not_use() -> None:
    """Only exportable items are cited - the same set the Export button downloads."""
    items = [
        _item("CONFLUENCE", "1", "Netflix", "https://wiki/1", exportable=True),
        _item("CONFLUENCE", "2", "Twitter", "https://wiki/2", exportable=False),
        _item("SHAREPOINT", "3", "n8n.pdf", "https://sp/3", exportable=False),
    ]
    footer = render_sources_footer(items)
    assert "Netflix" in footer
    assert "Twitter" not in footer and "n8n" not in footer


def test_footer_deduplicates_and_is_empty_when_nothing_was_used() -> None:
    items = [_item("CONFLUENCE", "1", "A", "https://wiki/1"), _item("CONFLUENCE", "1", "A", "https://wiki/1")]
    assert render_sources_footer(items).count("[A]") == 1
    assert render_sources_footer([]) == ""
    assert render_sources_footer([_item("CONFLUENCE", "1", "A", exportable=False)]) == ""


def test_append_is_idempotent_and_strip_restores_the_answer() -> None:
    items = [_item("CONFLUENCE", "1", "Netflix", "https://wiki/1")]
    answer = "Netflix is a streaming platform."
    once = append_sources_footer(answer, items)
    twice = append_sources_footer(once, items)
    assert once == twice
    assert has_sources_footer(once)
    assert strip_sources_footer(once) == answer
    assert not has_sources_footer(answer)
    # No sources -> the answer is returned unchanged (no dangling rule/heading).
    assert append_sources_footer(answer, []) == answer


def test_footer_reflects_relevance_promotion_from_the_capture() -> None:
    """Real flow: several search hits, the answer is about one of them."""
    capture = RetrievalCapture()
    for pid, title in [("1", "Netflix System Design and Architecture"), ("2", "E-Commerce Application System Design")]:
        capture.add(ExportItemDraft(source_type="CONFLUENCE", source_id=pid, source_name=title,
                                    source_url=f"https://wiki/{pid}", exportable=False))
    capture.promote_relevant_candidates("Tell me about the E-commerce architecture",
                                        "Here is the **E-Commerce System Design** overview.")
    footer = render_sources_footer(capture.items)
    assert "E-Commerce" in footer and "https://wiki/2" in footer
    assert "Netflix" not in footer
