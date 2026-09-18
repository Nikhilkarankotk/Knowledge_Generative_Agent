"""Tests for the shared retrieval evidence ledger and state-block helpers."""

from __future__ import annotations

import json

from app.retrieval.state import (
    MAX_EVIDENCE_ITEMS,
    RetrievalLedger,
    append_state,
    match_known_scope,
    state_end,
    state_start,
    strip_retrieval_state,
)


def _item(item_id: str, title: str = "", **extra) -> dict:
    item = {"id": item_id, "title": title or item_id}
    item.update(extra)
    return item


def test_render_is_empty_until_something_happens() -> None:
    assert RetrievalLedger("github").render() == ""


def test_evidence_survives_a_later_empty_search() -> None:
    ledger = RetrievalLedger("github", item_label="item")
    ledger.record_search("charge", [_item("acme/payments:src/pay.py", "src/pay.py")])
    ledger.record_search("charge architecture", [])

    state = ledger.render()

    assert "src/pay.py" in state
    assert '"last_search_results": 0' in state
    assert '"evidence_found": true' in state
    assert "does not erase them" in state


def test_search_error_keeps_prior_evidence_and_records_it() -> None:
    ledger = RetrievalLedger("confluence", item_label="page")
    ledger.record_search("roadmap", [_item("42", "Roadmap")])
    ledger.record_search_error("roadmap architecture", "boom")

    state = ledger.render()

    assert "Roadmap" in state
    assert "search failed: boom" in state
    assert '"evidence_found": true' in state


def test_record_retrieval_marks_retrieved_and_keeps_content() -> None:
    ledger = RetrievalLedger("sharepoint", item_label="document")
    ledger.record_retrieval("doc-1", _item("doc-1", "Policy"), "Policy body")

    state = ledger.render()

    assert '"retrieved": true' in state
    assert '"content_length": 11' in state
    assert "Policy body" in state


def test_content_unavailable_is_distinct_from_retrieved() -> None:
    ledger = RetrievalLedger("sharepoint", item_label="document")
    ledger.record_content_unavailable("doc-2")

    state = ledger.render()

    assert '"content_available": false' in state
    assert "content unavailable" in state


def test_richer_metadata_is_not_overwritten_by_a_sparse_hit() -> None:
    ledger = RetrievalLedger("confluence", item_label="page")
    ledger.record_search(
        "roadmap",
        [_item("42", "Roadmap", url="https://wiki/42", metadata={"space": "ENG"})],
    )
    ledger.record_search("roadmap", [_item("42", "Roadmap")])

    state = ledger.render()

    assert "https://wiki/42" in state
    assert '"space": "ENG"' in state


def test_item_cap_bounds_the_ledger() -> None:
    ledger = RetrievalLedger("github", item_label="item")
    ledger.record_search("many", [_item(f"repo-{index}") for index in range(MAX_EVIDENCE_ITEMS + 5)])

    payload = json.loads(ledger.render().splitlines()[2])

    assert payload["items_found"] == MAX_EVIDENCE_ITEMS


def test_strip_retrieval_state_removes_blocks_from_every_source() -> None:
    text = (
        "[Source: GitHub: a/b:src/x.py]\nURL: u\n\n"
        f"{state_start('github')} (authoritative evidence summary; machine-readable) ===\n"
        '{"source": "github"}\n'
        f"{state_end('github')}\n\n"
        "[Source: Confluence: Roadmap]\nURL: w\n\n"
        f"{state_start('confluence')} (authoritative evidence summary; machine-readable) ===\n"
        '{"source": "confluence"}\n'
        f"{state_end('confluence')}"
    )

    stripped = strip_retrieval_state(text)

    assert "RETRIEVAL STATE" not in stripped
    assert "src/x.py" in stripped
    assert "Roadmap" in stripped


def test_append_state_ignores_empty_state() -> None:
    assert append_state("result", "") == "result"
    assert append_state("result", "state").endswith("\n\nstate")


def test_match_known_scope_is_case_insensitive_and_rejects_unknown() -> None:
    known = {"arch": "ARCH", "pay": "PAY"}

    assert match_known_scope("arch", known) == "ARCH"
    assert match_known_scope("ARCH", known) == "ARCH"
    assert match_known_scope("mfs", known) is None
    assert match_known_scope(None, known) is None
    assert match_known_scope("arch", {}) is None
