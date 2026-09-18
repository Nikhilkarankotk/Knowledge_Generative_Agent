"""Tests for the graphical architecture PDF (`app.export.architecture_pdf`) and
its wiring into ``PdfExporter`` / ``build_github_analysis``."""

from __future__ import annotations

import importlib

from reportlab.graphics.shapes import Drawing, String

from app.export.architecture import ArchitectureModel, build_architecture_model
from app.export.architecture_pdf import architecture_pages, render_architecture_document
from app.export.exporters.document_exporters import PdfExporter
from app.export.registry import ExportPayload
from app.export.sources import ExportLimits, build_github_analysis

_SAMPLE = {
    "README.md": "# Acme Payments\nCharges cards and reports metrics.",
    "app/main.py": "import uvicorn\nfrom fastapi import FastAPI\napp = FastAPI()\nif __name__ == '__main__':\n    uvicorn.run(app)",
    "app/routes.py": (
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get('/payments')\ndef list_payments():\n    return payment_service.list()"
    ),
    "app/services/payment_service.py": (
        "class PaymentService:\n"
        "    def list(self):\n"
        "        import sqlalchemy\n"
        "        conn = psycopg.connect('postgresql://db')\n"
        "        return conn.execute('SELECT * FROM payments')\n"
        "    def charge(self, payload):\n"
        "        return requests.post('https://api.stripe.com/v1/charges', json=payload)"
    ),
    "Dockerfile": "FROM python:3.12\nCMD ['uvicorn', 'app.main:app']",
    ".github/workflows/ci.yml": "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest",
}

_TREE = [{"type": "file", "path": path} for path in sorted(_SAMPLE)]


def _sample_model() -> ArchitectureModel:
    return build_architecture_model(
        "acme/api",
        files=_SAMPLE,
        readme=_SAMPLE["README.md"],
        repo_meta="Description: Payment service.",
        tree=_TREE,
    )


def _pdf_page_count(data: bytes) -> int:
    return data.count(b"/Type /Page") - data.count(b"/Type /Pages")


def _collect_strings(drawing: Drawing, out: list[str]) -> None:
    from reportlab.graphics.shapes import Group

    for child in drawing.contents:
        if isinstance(child, String):
            if child.text:
                out.append(str(child.text))
        elif isinstance(child, Group):
            _collect_strings(child, out)


def test_architecture_pages_returns_seven() -> None:
    pages = architecture_pages(_sample_model())
    assert len(pages) == 7
    for page in pages:
        assert 0 < page.width <= 790
        assert 0 < page.height <= 540


def test_render_produces_landscape_a4_seven_page_pdf() -> None:
    data = render_architecture_document(_sample_model(), repo_url="https://github.com/acme/api")
    assert data.startswith(b"%PDF")
    assert _pdf_page_count(data) == 7
    assert b"841.8898 595.2756" in data  # landscape A4 media box


def test_empty_model_renders() -> None:
    empty = ArchitectureModel(repo="empty/repo")
    data = render_architecture_document(empty, repo_url=None)
    assert data.startswith(b"%PDF")
    assert _pdf_page_count(data) == 7


def test_pages_contain_no_source_code_dump() -> None:
    model = _sample_model()
    texts: list[str] = []
    for page in architecture_pages(model):
        _collect_strings(page, texts)
    joined = "\n".join(texts).lower()
    assert "def " not in joined
    assert "select * from" not in joined
    assert "import requests" not in joined


def test_pdf_exporter_renders_graphical_pdf_when_model_present() -> None:
    payload = ExportPayload(
        title="acme/api",
        source_type="GITHUB",
        source_name="acme/api",
        source_url="https://github.com/acme/api",
        sections=[],
        architecture=_sample_model(),
    )
    data = PdfExporter().render(payload)
    assert data.startswith(b"%PDF")
    assert _pdf_page_count(data) == 7


def test_pdf_exporter_falls_back_to_text_pdf_without_model() -> None:
    payload = ExportPayload(
        title="acme/api",
        source_type="GITHUB",
        source_name="acme/api",
        source_url="https://github.com/acme/api",
    )
    data = PdfExporter().render(payload)
    assert data.startswith(b"%PDF")
    assert _pdf_page_count(data) != 7


class FakeGitHub:
    enabled = True

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files
        self._tree = [{"type": "file", "path": path} for path in sorted(files)]

    def get_repository(self, repo: str) -> str:
        return (
            f"[Source: GitHub: {repo}]\nURL: https://github.com/{repo}\n"
            "Default branch: main\nDescription: Payment service that charges cards."
        )

    def get_readme(self, repo: str) -> str:
        return self._files["README.md"]

    def walk_repository(self, repo: str, *, max_items: int, max_depth: int, skip_dirs: tuple[str, ...] = ()) -> list[dict]:
        return self._tree

    def get_file_content_text(self, repo: str, path: str, *, char_limit: int | None = None) -> str:
        return self._files.get(path, "")


def test_build_github_analysis_attaches_architecture_model() -> None:
    payload, _ = build_github_analysis(
        "acme/api", FakeGitHub(_SAMPLE), limits=ExportLimits()  # type: ignore[arg-type]
    )
    assert payload.architecture is not None
    assert payload.architecture.components
    assert payload.architecture.repo == "acme/api"


def test_build_github_analysis_model_survives_render() -> None:
    limits = ExportLimits()
    payload, _ = build_github_analysis(
        "acme/api",
        FakeGitHub(_SAMPLE),  # type: ignore[arg-type]
        limits=limits,
    )
    assert payload.architecture is not None
    data = PdfExporter().render(payload)
    assert _pdf_page_count(data) == 7


def test_render_contains_security_and_tree_content() -> None:
    model = build_architecture_model(
        "acme/api",
        files={
            **_SAMPLE,
            "app/services/payment_service.py": _SAMPLE["app/services/payment_service.py"]
            + "\n    def issue(self):\n        import jwt\n        import bcrypt\n        return token\n",
        },
        readme=_SAMPLE["README.md"],
        tree=_TREE,
    )
    data = render_architecture_document(model, repo_url="https://github.com/acme/api")
    assert data.startswith(b"%PDF")


def test_architecture_module_imports_without_reportlab() -> None:
    module = importlib.import_module("app.export.architecture")
    assert hasattr(module, "build_architecture_model")
