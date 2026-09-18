"""End-to-end ExportService tests (context -> artifact)."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.export.errors import ExportNotAllowedError, ExportNotFoundError
from app.export.export_service import ExportService
from app.export.formats import ScenarioType
from app.export.intent import IntentRecommender
from app.export.sources import ExportLimits, ExportServices
from app.models import DocumentFile, ExportContext, ExportContextItem
from app.repositories import DocumentFileRepository, ExportContextRepository
from tests.conftest import make_pdf_with_text

SETTINGS = SimpleNamespace(export_include_summary=True)
LIMITS = ExportLimits()


class FakeDocumentFileRepo:
    def __init__(self, session) -> None:
        self._repo = DocumentFileRepository(session)

    def find_by_session_and_filename(self, session_id: str, filename: str) -> DocumentFile | None:
        return self._repo.find_by_session_and_filename(session_id, filename)


class FakeGitHub:
    enabled = True
    tree: list[dict] = []
    files: dict[str, str] = {}

    def get_repository(self, repo: str) -> str:
        return f"Name: {repo}\nDescription: payment service"

    def get_readme(self, repo: str) -> str:
        return "[Source: GitHub: acme/api:README.md]\nREADME of acme/api\nHandles payments"

    def walk_repository(
        self, repo: str, *, max_items: int, max_depth: int, skip_dirs: tuple[str, ...] = ()
    ) -> list[dict]:
        return self.tree

    def get_file_content_text(self, repo: str, path: str, *, char_limit: int | None = None) -> str:
        return self.files.get(path, "")


def _add_context(
    db_session, *, chat_message_id: int, session_id: str, expires_at: datetime | None = None
) -> ExportContext:
    context = ExportContext(
        chat_message_id=chat_message_id,
        session_id=session_id,
        created_at=datetime.now(),
        expires_at=expires_at,
        status="ready",
        source_count=1,
    )
    db_session.add(context)
    db_session.commit()
    return context


def _add_item(db_session, context: ExportContext, **kwargs) -> None:
    db_session.add(ExportContextItem(export_context_id=context.id, **kwargs))
    db_session.commit()


def _service(db_session, *, recommender=None):
    return ExportService(
        ExportContextRepository(db_session),
        SETTINGS,
        services=ExportServices(document_file_repo=FakeDocumentFileRepo(db_session)),
        recommender=recommender,
        limits=LIMITS,
    )


def test_native_upload_exports_original_bytes(db_session) -> None:
    session_id = "s-native"
    pdf = make_pdf_with_text("native policy pdf")
    db_session.add(
        DocumentFile(session_id=session_id, filename="policy.pdf", content=pdf, content_type="application/pdf")
    )
    db_session.commit()
    context = _add_context(db_session, chat_message_id=101, session_id=session_id)
    _add_item(
        db_session,
        context,
        source_type="UPLOADED_DOCUMENT",
        source_id="policy.pdf",
        source_name="policy.pdf",
        filename="policy.pdf",
        mime_type="application/pdf",
        content_reference="the policy contents",
        retrieval_rank=0,
        size_bytes=len(pdf),
    )

    artifact = _service(db_session).export(101, session_id, assistant_text="url: http://x")  # noqa: S106

    assert artifact.scenario == ScenarioType.NATIVE_FILE.value
    assert artifact.filename == "policy.pdf"
    assert artifact.data == pdf

    persisted = ExportContextRepository(db_session).find_by_chat_message_id(101)
    assert persisted is not None
    assert persisted.strategy == ScenarioType.NATIVE_FILE.value
    assert persisted.requested_format == "pdf"


def test_confluence_prose_generates_docx(db_session) -> None:
    context = _add_context(db_session, chat_message_id=102, session_id="s-conf")
    _add_item(
        db_session,
        context,
        source_type="CONFLUENCE",
        source_id="1234",
        source_name="Expense Policy",
        content_reference="Expense policy dictates monthly submission of receipts.",
        retrieval_rank=0,
    )

    artifact = _service(db_session).export(102, "s-conf")

    assert artifact.scenario == ScenarioType.GENERATED_DOCUMENT.value
    assert artifact.filename.endswith(".docx")
    assert artifact.data
    assert artifact.data.startswith(b"PK")


def test_github_generates_report(db_session) -> None:
    fake = FakeGitHub()
    fake.tree = [{"type": "file", "path": "src/main.py"}]
    fake.files = {"src/main.py": "@app.get('/health')\n"}

    context = _add_context(db_session, chat_message_id=103, session_id="s-gh")
    _add_item(
        db_session,
        context,
        source_type="GITHUB",
        source_id="acme/api",
        source_name="acme/api",
        content_reference="retrieved repo context",
        retrieval_rank=0,
    )

    service = ExportService(
        ExportContextRepository(db_session),
        SETTINGS,
        services=ExportServices(document_file_repo=FakeDocumentFileRepo(db_session), github_service=fake),
        limits=LIMITS,
    )
    artifact = service.export(103, "s-gh")

    assert artifact.scenario == ScenarioType.GENERATED_REPORT.value
    assert artifact.filename.endswith(".pdf")
    assert artifact.data.startswith(b"%PDF")


def test_multi_artifact_zip_with_summary(db_session) -> None:
    session_id = "s-multi"
    pdf = b"%PDF-multi"
    db_session.add(
        DocumentFile(session_id=session_id, filename="policy.pdf", content=pdf, content_type="application/pdf")
    )
    db_session.commit()
    context = _add_context(db_session, chat_message_id=104, session_id=session_id)
    _add_item(
        db_session,
        context,
        source_type="UPLOADED_DOCUMENT",
        source_id="policy.pdf",
        source_name="policy.pdf",
        filename="policy.pdf",
        mime_type="application/pdf",
        content_reference="policy text",
        retrieval_rank=0,
    )
    _add_item(
        db_session,
        context,
        source_type="CONFLUENCE",
        source_id="9",
        source_name="Onboarding",
        content_reference="Onboarding covers account setup.",
        retrieval_rank=1,
    )

    artifact = _service(db_session).export(104, session_id, assistant_text="Summary of retrieved docs.")

    assert artifact.scenario == ScenarioType.MULTI_ARTIFACT.value
    assert artifact.path is not None and artifact.path.endswith(".zip")
    assert artifact.cleanup_dir is not None
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            names = archive.namelist()
            assert names[0] == "manifest.json"
            assert "uploaded_documents/policy.pdf" in names
            assert "confluence/Onboarding.docx" in names
            assert "export-summary.md" in names
            assert archive.read("uploaded_documents/policy.pdf") == pdf
    finally:
        import shutil

        if artifact.cleanup_dir:
            shutil.rmtree(artifact.cleanup_dir, ignore_errors=True)


def test_github_multi_entry_contains_analysis_and_sources(db_session) -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "dir", "path": "src"},
        {"type": "file", "path": "src/app.py"},
        {"type": "file", "path": "requirements.txt"},
    ]
    fake.files = {
        "src/app.py": 'HTTP.get("/ping")\ndef main():\n    return "ok"' * 3,
        "requirements.txt": "fastapi",
    }
    context = _add_context(db_session, chat_message_id=105, session_id="s-gh2")
    _add_item(
        db_session,
        context,
        source_type="GITHUB",
        source_id="acme/api",
        source_name="acme/api",
        content_reference="repo",
        retrieval_rank=0,
    )
    _add_item(
        db_session,
        context,
        source_type="CONFLUENCE",
        source_id="5",
        source_name="Docs",
        content_reference="Imported docs.",
        retrieval_rank=1,
    )

    service = ExportService(
        ExportContextRepository(db_session),
        SETTINGS,
        services=ExportServices(github_service=fake),
        limits=LIMITS,
    )
    artifact = service.export(105, "s-gh2")
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            names = set(archive.namelist())
            assert any(name.startswith("github/api/analysis.") for name in names)
            assert any("/source/src/app.py" in name for name in names)
    finally:
        import shutil

        if artifact.cleanup_dir:
            shutil.rmtree(artifact.cleanup_dir, ignore_errors=True)


def test_confluence_nested_page_export_regression(db_session) -> None:
    """The bug: 'API Documentation' nested under 'Payments Application' was missed.

    Once the page is retrieved, the export pipeline must generate and name the
    artifact from the retrieved source name ("API Documentation") under the
    ``confluence/`` prefix and keep the Confluence attribution in the manifest --
    the source identity must survive end-to-end with the capture changes.
    """
    session_id = "s-api-doc"
    db_session.add(
        DocumentFile(session_id=session_id, filename="policy.pdf", content=b"%PDF", content_type="application/pdf")
    )
    db_session.commit()
    meta = {"space": "PAY", "parent": "Payments Application"}
    context = _add_context(db_session, chat_message_id=110, session_id=session_id)
    _add_item(
        db_session,
        context,
        source_type="UPLOADED_DOCUMENT",
        source_id="policy.pdf",
        source_name="policy.pdf",
        filename="policy.pdf",
        mime_type="application/pdf",
        content_reference="policy text",
        retrieval_rank=1,
    )
    _add_item(
        db_session,
        context,
        source_type="CONFLUENCE",
        source_id="DOC1",
        source_name="API Documentation",
        source_url="https://wiki.example.com/spaces/PAY/pages/DOC1",
        meta=meta,
        content_reference="REST endpoint reference for the Payments APIs.",
        retrieval_rank=0,
    )

    artifact = _service(db_session).export(110, session_id)

    assert artifact.scenario == ScenarioType.MULTI_ARTIFACT.value
    try:
        with zipfile.ZipFile(artifact.path) as archive:
            names = archive.namelist()
            assert names[0] == "manifest.json"
            assert "confluence/API Documentation.docx" in names
            manifest = json.loads(archive.read("manifest.json"))
            assert any(
                entry.get("source_type") == "CONFLUENCE"
                and entry.get("source_name") == "API Documentation"
                for entry in manifest.get("entries", [])
            )
    finally:
        import shutil

        if artifact.cleanup_dir:
            shutil.rmtree(artifact.cleanup_dir, ignore_errors=True)


def test_export_rejects_other_session(db_session) -> None:
    _add_context(db_session, chat_message_id=106, session_id="owner-session")
    _add_item(
        db_session,
        ExportContextRepository(db_session).find_by_chat_message_id(106),
        source_type="CONFLUENCE",
        source_id="1",
        source_name="P",
        content_reference="c",
        retrieval_rank=0,
    )
    with pytest.raises(ExportNotAllowedError):
        _service(db_session).export(106, "intruder-session")


def test_export_missing_context_raises_not_found(db_session) -> None:
    with pytest.raises(ExportNotFoundError):
        _service(db_session).export(99999, "whatever")


def test_export_expired_context_raises_not_found(db_session) -> None:
    context = _add_context(
        db_session,
        chat_message_id=107,
        session_id="s-exp",
        expires_at=datetime.now() - timedelta(minutes=5),
    )
    _add_item(
        db_session,
        context,
        source_type="CONFLUENCE",
        source_id="1",
        source_name="P",
        content_reference="c",
        retrieval_rank=0,
    )
    with pytest.raises(ExportNotFoundError):
        _service(db_session).export(107, "s-exp")


def test_export_without_items_raises_not_found(db_session) -> None:
    _add_context(db_session, chat_message_id=108, session_id="s-empty")
    with pytest.raises(ExportNotFoundError):
        _service(db_session).export(108, "s-empty")


def test_single_native_hard_rule_beats_llm_recommendation(db_session) -> None:
    session_id = "s-native2"
    db_session.add(
        DocumentFile(session_id=session_id, filename="policy.pdf", content=b"%PDF", content_type="application/pdf")
    )
    db_session.commit()
    context = _add_context(db_session, chat_message_id=109, session_id=session_id)
    _add_item(
        db_session,
        context,
        source_type="UPLOADED_DOCUMENT",
        source_id="policy.pdf",
        source_name="policy.pdf",
        filename="policy.pdf",
        mime_type="application/pdf",
        content_reference="policy",
        retrieval_rank=0,
    )

    recommender = Mock(wraps=IntentRecommender(Mock()))
    recommender.enabled = True
    recommender.recommend.return_value = None

    artifact = _service(db_session, recommender=recommender).export(109, session_id)

    assert artifact.scenario == ScenarioType.NATIVE_FILE.value
    assert artifact.data == b"%PDF"


# --- session-wide, query-organized export -------------------------------------


def _session_service(db_session):
    from app.repositories import ChatMessageRepository

    return ExportService(
        ExportContextRepository(db_session),
        SETTINGS,
        services=ExportServices(document_file_repo=FakeDocumentFileRepo(db_session)),
        limits=LIMITS,
        chat_repo=ChatMessageRepository(db_session),
    )


def _add_turn(db_session, session_id: str, question: str, answer: str):
    """Persist a user question + assistant answer; return the assistant message."""
    from app.models import ChatMessage

    user = ChatMessage(session_id=session_id, role="user", content=question, timestamp=datetime.now())
    db_session.add(user)
    db_session.commit()
    assistant = ChatMessage(session_id=session_id, role="assistant", content=answer, timestamp=datetime.now())
    db_session.add(assistant)
    db_session.commit()
    return assistant


def _read_zip(artifact) -> tuple[list[str], dict]:
    with zipfile.ZipFile(artifact.path) as archive:
        names = archive.namelist()
        metadata = json.loads(archive.read("metadata.json"))
    return names, metadata


def _cleanup(artifact) -> None:
    import shutil

    if artifact.cleanup_dir:
        shutil.rmtree(artifact.cleanup_dir, ignore_errors=True)


def test_session_export_organizes_by_query_and_dedupes_pages(db_session) -> None:
    session_id = "s-session"

    # Query 1 used pages A and B; Query 2 used pages B (again) and C.
    a1 = _add_turn(db_session, session_id, "How does the CDN work?", "The CDN caches at the edge.")
    c1 = _add_context(db_session, chat_message_id=a1.id, session_id=session_id)
    _add_item(db_session, c1, source_type="CONFLUENCE", source_id="A", source_name="Page A",
              source_url="https://conf/A", content_reference="Content of page A", retrieval_rank=0)
    _add_item(db_session, c1, source_type="CONFLUENCE", source_id="B", source_name="Page B",
              source_url="https://conf/B", content_reference="Content of page B", retrieval_rank=1)

    a2 = _add_turn(db_session, session_id, "How do we scale?", "Autoscaling handles peaks.")
    c2 = _add_context(db_session, chat_message_id=a2.id, session_id=session_id)
    _add_item(db_session, c2, source_type="CONFLUENCE", source_id="B", source_name="Page B",
              source_url="https://conf/B", content_reference="Content of page B", retrieval_rank=0)
    _add_item(db_session, c2, source_type="CONFLUENCE", source_id="C", source_name="Page C",
              source_url="https://conf/C", content_reference="Content of page C", retrieval_rank=1)

    artifact = _session_service(db_session).export_session(session_id)
    try:
        assert artifact.filename == "Export.zip"
        assert artifact.mime_type == "application/zip"
        names, metadata = _read_zip(artifact)

        # ChatHistory: one response document per query.
        assert "ChatHistory/Query1_Response.docx" in names
        assert "ChatHistory/Query2_Response.docx" in names

        # Confluence: pages grouped under the query that used them.
        assert "Confluence/Query1/Page A.docx" in names
        assert "Confluence/Query1/Page B.docx" in names
        assert "Confluence/Query2/Page C.docx" in names
        # Page B was already exported under Query1 -> NOT duplicated under Query2.
        assert "Confluence/Query2/Page B.docx" not in names
        assert sum(1 for n in names if n.endswith("Page B.docx")) == 1

        # metadata.json: query -> source mapping.
        assert metadata["session_id"] == session_id
        assert metadata["query_count"] == 2
        assert metadata["unique_source_count"] == 3
        q1, q2 = metadata["queries"]
        assert q1["label"] == "Query1"
        assert q1["user_query"] == "How does the CDN work?"
        assert q1["response_file"] == "ChatHistory/Query1_Response.docx"
        assert {s["source_id"] for s in q1["sources"]} == {"A", "B"}
        assert all(s["referenced_from_earlier_query"] is False for s in q1["sources"])

        assert q2["label"] == "Query2"
        assert q2["user_query"] == "How do we scale?"
        assert {s["source_id"] for s in q2["sources"]} == {"B", "C"}
        page_b = next(s for s in q2["sources"] if s["source_id"] == "B")
        # Query2 still records that it USED page B, pointing at the single copy.
        assert page_b["referenced_from_earlier_query"] is True
        assert page_b["file"] == "Confluence/Query1/Page B.docx"
    finally:
        _cleanup(artifact)


def test_session_export_response_docx_contains_question_and_answer(db_session) -> None:
    session_id = "s-session-doc"
    assistant = _add_turn(db_session, session_id, "What is Netflix?", "Netflix is a streaming platform.")
    context = _add_context(db_session, chat_message_id=assistant.id, session_id=session_id)
    _add_item(db_session, context, source_type="CONFLUENCE", source_id="N", source_name="Netflix",
              content_reference="Netflix page", retrieval_rank=0)

    artifact = _session_service(db_session).export_session(session_id)
    try:
        from io import BytesIO

        from docx import Document

        with zipfile.ZipFile(artifact.path) as archive:
            doc = Document(BytesIO(archive.read("ChatHistory/Query1_Response.docx")))
            text = "\n".join(p.text for p in doc.paragraphs)
        assert "What is Netflix?" in text
        assert "Netflix is a streaming platform." in text
    finally:
        _cleanup(artifact)


def test_session_export_only_includes_pages_used_by_each_query(db_session) -> None:
    session_id = "s-session-scope"
    a1 = _add_turn(db_session, session_id, "Q1", "A1")
    c1 = _add_context(db_session, chat_message_id=a1.id, session_id=session_id)
    _add_item(db_session, c1, source_type="CONFLUENCE", source_id="X", source_name="Page X",
              content_reference="x", retrieval_rank=0)
    a2 = _add_turn(db_session, session_id, "Q2", "A2")
    c2 = _add_context(db_session, chat_message_id=a2.id, session_id=session_id)
    _add_item(db_session, c2, source_type="CONFLUENCE", source_id="Y", source_name="Page Y",
              content_reference="y", retrieval_rank=0)

    artifact = _session_service(db_session).export_session(session_id)
    try:
        names, metadata = _read_zip(artifact)
        # Each query's folder holds ONLY the page that query actually used.
        assert [n for n in names if n.startswith("Confluence/Query1/")] == ["Confluence/Query1/Page X.docx"]
        assert [n for n in names if n.startswith("Confluence/Query2/")] == ["Confluence/Query2/Page Y.docx"]
        assert [s["source_id"] for s in metadata["queries"][0]["sources"]] == ["X"]
        assert [s["source_id"] for s in metadata["queries"][1]["sources"]] == ["Y"]
    finally:
        _cleanup(artifact)


def test_session_export_is_session_isolated(db_session) -> None:
    other = _add_turn(db_session, "other-session", "Q", "A")
    ctx = _add_context(db_session, chat_message_id=other.id, session_id="other-session")
    _add_item(db_session, ctx, source_type="CONFLUENCE", source_id="Z", source_name="Z",
              content_reference="z", retrieval_rank=0)

    with pytest.raises(ExportNotFoundError):
        _session_service(db_session).export_session("empty-session")


def test_session_export_requires_chat_repo(db_session) -> None:
    from app.export.errors import ExportValidationError

    with pytest.raises(ExportValidationError):
        _service(db_session).export_session("any")


# --- sources-only export for ONE response (the chat "Export" button) -----------


def test_response_sources_export_contains_only_that_responses_confluence_pages(db_session) -> None:
    session_id = "s-one"

    # An EARLIER query that used page OLD - must NOT appear in the export.
    a1 = _add_turn(db_session, session_id, "Earlier question", "Earlier answer")
    c1 = _add_context(db_session, chat_message_id=a1.id, session_id=session_id)
    _add_item(db_session, c1, source_type="CONFLUENCE", source_id="OLD", source_name="Old Page",
              content_reference="old content", retrieval_rank=0)

    # The LATEST response used pages A and B (B retrieved as two chunks).
    a2 = _add_turn(db_session, session_id, "Latest question", "Latest answer")
    c2 = _add_context(db_session, chat_message_id=a2.id, session_id=session_id)
    _add_item(db_session, c2, source_type="CONFLUENCE", source_id="A", source_name="Page A",
              source_url="https://conf/A", content_reference="Content A", retrieval_rank=0)
    _add_item(db_session, c2, source_type="CONFLUENCE", source_id="B", source_name="Page B",
              source_url="https://conf/B", content_reference="Content B part 1", retrieval_rank=1)
    _add_item(db_session, c2, source_type="CONFLUENCE", source_id="B", source_name="Page B",
              source_url="https://conf/B", content_reference="Content B part 2", retrieval_rank=2)

    artifact = _service(db_session).export_response_sources(a2.id, session_id)
    try:
        # Only Confluence contributed -> archive named after that one system.
        assert artifact.filename == "confluence.zip"
        assert artifact.mime_type == "application/zip"
        names, metadata = _read_zip(artifact)

        # Only the two pages used for THIS response, each exactly once.
        assert sorted(n for n in names if n.startswith("Confluence/")) == [
            "Confluence/Page A.docx",
            "Confluence/Page B.docx",
        ]
        # No chat history, no earlier query's page.
        assert not any(n.startswith("ChatHistory/") for n in names)
        assert not any("Old Page" in n for n in names)

        assert metadata["scope"] == "single_response_sources"
        assert metadata["chat_message_id"] == a2.id
        assert metadata["source_count"] == 2
        assert metadata["source_types"] == ["CONFLUENCE"]
        assert {s["source_id"] for s in metadata["sources"]} == {"A", "B"}
    finally:
        _cleanup(artifact)


def test_response_sources_export_mixed_systems_is_knowledge_export_zip(db_session) -> None:
    """E-commerce answer grounded in a Confluence page AND a GitHub repo ->
    both are exported, organised by system, as 'Knowledge Export.zip'."""
    session_id = "s-mixed"
    assistant = _add_turn(db_session, session_id, "E-commerce architecture?", "Combined answer")
    context = _add_context(db_session, chat_message_id=assistant.id, session_id=session_id)
    _add_item(db_session, context, source_type="CONFLUENCE", source_id="2785284",
              source_name="E-Commerce Application System Design", content_reference="conf page",
              retrieval_rank=0)
    _add_item(db_session, context, source_type="GITHUB", source_id="acme/E-commerce_Application",
              source_name="acme/E-commerce_Application", content_reference="repo readme", retrieval_rank=1)

    artifact = _service(db_session).export_response_sources(assistant.id, session_id)
    try:
        assert artifact.filename == "Knowledge Export.zip"
        names, metadata = _read_zip(artifact)
        assert "Confluence/E-Commerce Application System Design.docx" in names
        assert any(n.startswith("GitHub/") and n.endswith("/analysis.docx") or n.startswith("GitHub/") for n in names)
        assert metadata["source_types"] == ["CONFLUENCE", "GITHUB"]
        assert {s["source_type"] for s in metadata["sources"]} == {"CONFLUENCE", "GITHUB"}
    finally:
        _cleanup(artifact)


def test_response_sources_export_github_only_is_github_zip(db_session) -> None:
    session_id = "s-gh-only"
    assistant = _add_turn(db_session, session_id, "Q", "A")
    context = _add_context(db_session, chat_message_id=assistant.id, session_id=session_id)
    _add_item(db_session, context, source_type="GITHUB", source_id="acme/api", source_name="acme/api",
              content_reference="repo", retrieval_rank=0)

    artifact = _service(db_session).export_response_sources(assistant.id, session_id)
    try:
        assert artifact.filename == "github.zip"
        names, metadata = _read_zip(artifact)
        assert all(n.startswith("GitHub/") or n.endswith(".json") for n in names)
        assert metadata["source_types"] == ["GITHUB"]
    finally:
        _cleanup(artifact)


def test_response_sources_export_sharepoint_only_is_sharepoint_zip(db_session) -> None:
    session_id = "s-sp-only"
    assistant = _add_turn(db_session, session_id, "Q", "A")
    context = _add_context(db_session, chat_message_id=assistant.id, session_id=session_id)
    _add_item(db_session, context, source_type="SHAREPOINT", source_id="doc-1", source_name="Policy.docx",
              content_reference="policy text", retrieval_rank=0)

    artifact = _service(db_session).export_response_sources(assistant.id, session_id)
    try:
        assert artifact.filename == "sharepoint.zip"
        names, _ = _read_zip(artifact)
        assert any(n.startswith("SharePoint/") for n in names)
    finally:
        _cleanup(artifact)


def test_response_sources_export_rejects_other_session(db_session) -> None:
    assistant = _add_turn(db_session, "owner", "Q", "A")
    context = _add_context(db_session, chat_message_id=assistant.id, session_id="owner")
    _add_item(db_session, context, source_type="CONFLUENCE", source_id="P", source_name="P",
              content_reference="p", retrieval_rank=0)

    with pytest.raises(ExportNotAllowedError):
        _service(db_session).export_response_sources(assistant.id, "intruder")


def test_find_latest_exportable_message_falls_back_past_answers_without_sources(db_session) -> None:
    """After a refresh the last answer may have no Confluence sources (e.g. a
    general-knowledge reply); Export must still target the newest answer that does."""
    sid = "s-latest-fb"
    a1 = _add_turn(db_session, sid, "Q1", "A1 from Confluence")
    c1 = _add_context(db_session, chat_message_id=a1.id, session_id=sid)
    _add_item(db_session, c1, source_type="CONFLUENCE", source_id="P", source_name="Page P",
              content_reference="p", retrieval_rank=0, exportable=True)
    # Newest answer: no export context at all (nothing was retrieved).
    a2 = _add_turn(db_session, sid, "Q2", "General answer")

    svc = _service(db_session)
    assert svc.find_latest_exportable_message(sid) == a1.id
    # Asking for the source-less latest message still resolves to a1.
    assert svc.find_latest_exportable_message(sid, preferred_chat_message_id=a2.id) == a1.id
    # A qualifying preferred id is honoured as-is.
    assert svc.find_latest_exportable_message(sid, preferred_chat_message_id=a1.id) == a1.id


def test_find_latest_exportable_message_ignores_search_only_candidates(db_session) -> None:
    sid = "s-latest-cand"
    a1 = _add_turn(db_session, sid, "Q1", "A1")
    c1 = _add_context(db_session, chat_message_id=a1.id, session_id=sid)
    # Found by search but never read -> not exportable.
    _add_item(db_session, c1, source_type="CONFLUENCE", source_id="X", source_name="X",
              content_reference="x", retrieval_rank=0, exportable=False)

    assert _service(db_session).find_latest_exportable_message(sid) is None


def test_find_latest_exportable_message_is_session_scoped(db_session) -> None:
    other = _add_turn(db_session, "other", "Q", "A")
    ctx = _add_context(db_session, chat_message_id=other.id, session_id="other")
    _add_item(db_session, ctx, source_type="CONFLUENCE", source_id="P", source_name="P",
              content_reference="p", retrieval_rank=0)

    assert _service(db_session).find_latest_exportable_message("mine") is None
