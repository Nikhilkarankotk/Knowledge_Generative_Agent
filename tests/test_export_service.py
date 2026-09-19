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
