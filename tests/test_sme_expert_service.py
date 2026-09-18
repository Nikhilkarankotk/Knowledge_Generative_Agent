"""Tests for the SME/Expert resolver (dashboard Top Experts)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.models import ExportContext, ExportContextItem
from app.repositories import ExportContextRepository
from app.services.sme_expert_service import (
    ROLE_CONTRIBUTOR,
    ROLE_OWNER,
    ROLE_RECENT_EDITOR,
    SmeExpertService,
    resolve_experts,
)


def _item(source_type: str, source_id: str, name: str | None = None, **meta):
    return SimpleNamespace(
        source_type=source_type,
        source_id=source_id,
        source_name=name or source_id,
        meta=meta,
    )


def test_owner_is_primary_expert() -> None:
    items = [_item("CONFLUENCE", "1", "Payments Architecture", owner="Alice Doe")]
    (expert,) = resolve_experts(items)
    assert expert.name == "Alice Doe"
    assert expert.role == ROLE_OWNER
    assert expert.source == "Confluence"
    assert expert.source_count == 1
    assert "Payments Architecture" in expert.reason
    assert expert.initials == "AD"


def test_recent_editor_is_second_priority() -> None:
    items = [
        _item("CONFLUENCE", "1", "Rates", owner="Alice Doe"),
        _item("CONFLUENCE", "2", "Rates II", last_editor="Bob Ray"),
    ]
    experts = resolve_experts(items)
    assert [expert.role for expert in experts] == [ROLE_OWNER, ROLE_RECENT_EDITOR]
    assert experts[1].name == "Bob Ray"


def test_owner_not_duplicated_as_editor() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "1",
            "Doc",
            owner="Alice Doe",
            last_editor="Alice Doe",
        )
    ]
    (expert,) = resolve_experts(items)
    assert expert.role == ROLE_OWNER
    assert expert.source_count == 1


def test_prefers_person_associated_with_multiple_sources() -> None:
    items = [
        _item("CONFLUENCE", "1", "Doc A", owner="Alice Doe"),
        _item("CONFLUENCE", "2", "Doc B", owner="Alice Doe"),
        _item("CONFLUENCE", "3", "Doc C", owner="Zed Young"),
    ]
    experts = resolve_experts(items)
    assert experts[0].name == "Alice Doe"
    assert experts[0].source_count == 2
    assert experts[1].name == "Zed Young"


def test_contributors_are_ranked_below_owner_and_editor() -> None:
    items = [
        _item(
            "SHAREPOINT",
            "doc1",
            "Spec",
            owner="Alice",
            last_editor="Bob",
            contributors=["Cara Lin"],
        )
    ]
    experts = resolve_experts(items)
    assert [expert.name for expert in experts] == ["Alice", "Bob", "Cara Lin"]
    assert experts[2].role == ROLE_CONTRIBUTOR
    assert experts[2].source == "SharePoint"


def test_github_repo_owner_is_a_contributor() -> None:
    (expert,) = resolve_experts([_item("GITHUB", "acme/payments", "acme/payments")])
    assert expert.name == "acme"
    assert expert.role == ROLE_CONTRIBUTOR
    assert expert.source == "GitHub"
    assert expert.source_count == 1


def test_recent_editors_from_version_history_become_candidates() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "9000",
            "Netflix System Design and Implementation",
            owner="Nikhil Karankot",
            last_editor="Tejaswinik",
            recent_editors=["Tejaswinik", "Nikhil Karankot"],
        )
    ]
    experts = resolve_experts(items)
    assert [expert.name for expert in experts] == ["Nikhil Karankot", "Tejaswinik"]
    assert experts[0].role == ROLE_OWNER
    assert experts[1].role == ROLE_RECENT_EDITOR


def test_owner_also_latest_editor_is_a_single_combined_entry() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "9000",
            "Doc",
            owner="Nikhil Karankot",
            last_editor="Nikhil Karankot",
            recent_editors=["Nikhil Karankot"],
        )
    ]
    experts = resolve_experts(items)
    assert len(experts) == 1
    assert experts[0].name == "Nikhil Karankot"
    assert experts[0].role == ROLE_OWNER
    assert set(experts[0].roles) == {ROLE_OWNER, ROLE_RECENT_EDITOR}


def test_owner_is_not_discarded_when_recent_editors_exist() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "1",
            "Doc",
            owner="Owner Person",
            recent_editors=["Editor One", "Editor Two"],
        )
    ]
    names = [expert.name for expert in resolve_experts(items, max_experts=3)]
    assert names == ["Owner Person", "Editor One", "Editor Two"]


def test_candidates_are_combined_across_relevant_pages() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "1",
            "Page A",
            owner="Alice",
            last_editor="Bob",
            recent_editors=["Bob", "Alice"],
            modified="2026-01-01T00:00:00Z",
        ),
        _item(
            "CONFLUENCE",
            "2",
            "Page B",
            owner="Cara",
            last_editor="Bob",
            recent_editors=["Bob"],
            modified="2026-02-01T00:00:00Z",
        ),
    ]
    experts = resolve_experts(items, max_experts=3)
    names = [expert.name for expert in experts]
    assert set(names[:2]) == {"Alice", "Cara"}
    bob = next(expert for expert in experts if expert.name == "Bob")
    assert bob.source_count == 2


def test_more_recent_editors_rank_above_older_ones() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "1",
            "Older Page",
            owner="Alice",
            last_editor="Old Editor",
            recent_editors=["Old Editor"],
            modified="2025-01-01T00:00:00Z",
        ),
        _item(
            "CONFLUENCE",
            "2",
            "Newer Page",
            owner="Alice",
            last_editor="New Editor",
            recent_editors=["New Editor"],
            modified="2026-06-01T00:00:00Z",
        ),
    ]
    experts = resolve_experts(items, max_experts=3)
    names = [expert.name for expert in experts]
    assert names.index("New Editor") < names.index("Old Editor")


def test_recent_editor_details_rank_by_individual_edit_time() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "1",
            "Page",
            owner="Alice",
            recent_editors=["Old Editor", "New Editor"],
            recent_editor_details=[
                {"name": "Old Editor", "when": "2025-01-01T00:00:00Z", "version": "1"},
                {"name": "New Editor", "when": "2026-06-01T00:00:00Z", "version": "5"},
            ],
        )
    ]
    names = [expert.name for expert in resolve_experts(items, max_experts=3)]
    assert names.index("New Editor") < names.index("Old Editor")


def test_confluence_and_sharepoint_contributors_are_combined() -> None:
    items = [
        _item(
            "CONFLUENCE",
            "1",
            "Confluence Doc",
            owner="Alice",
            recent_editors=["Bob"],
        ),
        _item(
            "SHAREPOINT",
            "doc1",
            "SharePoint Spec",
            owner="Cara",
            last_editor="Alice",
        ),
    ]
    names = [expert.name for expert in resolve_experts(items, max_experts=3)]
    assert names == ["Alice", "Cara", "Bob"]


def test_no_person_metadata_yields_no_experts() -> None:
    assert resolve_experts([_item("CONFLUENCE", "1", "Doc")]) == []


def test_no_metadata_yields_no_experts() -> None:
    assert resolve_experts([_item("UPLOADED_DOCUMENT", "report.pdf")]) == []
    assert resolve_experts([]) == []


def test_blank_names_are_never_used() -> None:
    items = [_item("CONFLUENCE", "1", "Doc", owner="   ", last_editor="")]
    assert resolve_experts(items) == []


def test_returns_at_most_three_experts() -> None:
    items = [
        _item("CONFLUENCE", str(index), f"Doc {index}", owner=f"Person {index}")
        for index in range(5)
    ]
    assert len(resolve_experts(items)) == 3


def _seed_context(
    repo: ExportContextRepository,
    *,
    chat_message_id: int,
    session_id: str,
    meta: dict | None = None,
) -> None:
    context = ExportContext(
        chat_message_id=chat_message_id,
        session_id=session_id,
        created_at=datetime.now(),
        status="ready",
        source_count=1,
    )
    repo.save(context)
    repo.save_item(
        ExportContextItem(
            export_context_id=context.id,
            source_type="CONFLUENCE",
            source_id="1",
            source_name="Payments Architecture",
            meta=meta or {},
            retrieval_rank=0,
            exportable=True,
        )
    )


def test_service_resolves_latest_context_for_session(db_session) -> None:
    repo = ExportContextRepository(db_session)
    _seed_context(
        repo,
        chat_message_id=10,
        session_id="s1",
        meta={"owner": "Alice Doe"},
    )
    result = SmeExpertService(repo).resolve("s1")
    assert result["chatMessageId"] == 10
    assert result["experts"][0]["name"] == "Alice Doe"
    assert result["experts"][0]["role"] == ROLE_OWNER


def test_service_rejects_message_from_another_session(db_session) -> None:
    repo = ExportContextRepository(db_session)
    _seed_context(
        repo,
        chat_message_id=11,
        session_id="owner",
        meta={"owner": "Alice Doe"},
    )
    result = SmeExpertService(repo).resolve("intruder", chat_message_id=11)
    assert result["experts"] == []


def test_service_without_context_returns_empty(db_session) -> None:
    repo = ExportContextRepository(db_session)
    assert SmeExpertService(repo).resolve("never") == {
        "chatMessageId": None,
        "experts": [],
    }
