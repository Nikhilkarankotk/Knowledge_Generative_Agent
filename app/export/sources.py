"""Source resolvers - turn persisted retrieval items into concrete artifacts.

Each :class:`app.models.export_context.ExportContextItem` is resolved into a
:class:`ResolvedSource` that the ExportService can hand to an exporter:

* ``UPLOADED_DOCUMENT`` - the stored original bytes (``document_file``) when they
  are available (native), otherwise the retrieved chunks captured at query time.
* ``CONFLUENCE`` - the page content; the live page is re-fetched once when the
  Confluence service is enabled (same endpoint the plugin uses), falling back to
  the captured excerpt when it is unavailable.
* ``SHAREPOINT`` - the original bytes via the Graph download endpoint (same access
  control as reads), falling back to captured content.
* ``GITHUB`` - a bounded re-analysis of the allowlisted repository through the
  GitHub service. The analysis is evidence-driven: every section is labelled
  ``Observed from source`` / ``Inferred from source structure`` / ``Unavailable``;
  sensitive files (credentials, keys, ``.env*``) are never read or exported; a
  sampled set of key source files is produced for archive ``source/`` folders.

Resolvers never accept artifact identifiers from a prompt: they only act on rows
that were persisted from actual plugin retrieval and validated against the same
service allowlists at retrieval time.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.export.capture import parse_confluence_page
from app.export.diagrams import LAYER_TITLES, ArchitectureLayer, render_architecture_text
from app.export.formats import SourceType
from app.export.registry import ExportPayload, ExportSection
from app.models import ExportContextItem
from app.repositories import DocumentFileRepository
from app.services.confluence_service import ConfluenceService
from app.services.github_service import GitHubService
from app.services.sharepoint_service import SharePointService

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Sensitive-path guard for GitHub source sampling. Any path matching one of
# these markers (case-insensitive) is never read, never sampled and never
# exported - mirroring the requirement that credentials/keys are excluded.
_SENSITIVE_MARKERS = (
    ".env",
    ".git/",
    "secret",
    "credential",
    "token",
    ".pem",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    ".key",
    ".p12",
    ".pfx",
    ".jks",
    ".htpasswd",
    "passwords",
    "aws_credentials",
    ".npmrc",
    "pip.conf",
    "application.properties.local",
    "local.properties",
)

_KEY_BUILD_FILES = (
    "package.json",
    "pom.xml",
    "build.gradle",
    "settings.gradle",
    "build.gradle.kts",
    "settings.gradle.kts",
    "requirements.txt",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "Gopkg.toml",
    "setup.py",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "application.yml",
    "application.yaml",
    "application.properties",
    ".github/workflows/ci.yml",
    ".gitlab-ci.yml",
    "Jenkinsfile",
)

_CODE_EXTENSIONS = (
    ".py",
    ".java",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".go",
    ".rs",
    ".kt",
    ".cs",
    ".rb",
    ".php",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".vue",
)

_ROUTE_PATTERNS = (
    re.compile(r"@(?:app|router)\.(get|post|put|patch|delete)\("),
    re.compile(r"@(?:Get|Post|Put|Patch|Delete|Request)Mapping\("),
    re.compile(r"\.(?:get|post|put|delete|patch)\s*\(\s*['\"]"),
    re.compile(r"req\.(?:get|post|put|delete|patch)\s*\("),
    re.compile(r"Route::(?:get|post|put|delete|patch)\("),
    re.compile(r"HTTP\.(?:GET|POST|PUT|DELETE|PATCH)\s*\(|register_(?:get|post|put|delete)\s*\("),
)

# Entry-point file names and in-code markers. Used to prioritize which files are
# read first and to trace where the application starts in the code-flow section.
_ENTRY_BASENAMES = {
    "main.py", "__main__.py", "app.py", "application.py", "manage.py", "run.py",
    "server.py", "wsgi.py", "asgi.py", "cli.py", "main.js", "app.js", "server.js",
    "index.js", "main.jsx", "app.jsx", "index.jsx", "main.ts", "app.ts",
    "server.ts", "index.ts", "main.tsx", "app.tsx", "index.tsx", "main.go",
    "main.rs", "main.java", "application.java", "main.kt", "application.kt",
}
_ENTRY_CONTENT_MARKERS = (
    "if __name__",
    "uvicorn.run",
    "create_app",
    "app.run(",
    "def main(",
    "func main(",
    "fn main(",
    "public static void main",
    "@SpringBootApplication",
    "app.listen(",
    "server.listen(",
    "Application.Run(",
    "HTTPServer",
)
# Basename/layer hints that make a file worth reading early (services, routes, ...).
_LAYER_HINTS = (
    "controller", "service", "repository", "routes", "router", "api", "handler",
    "views", "models", "schema", "dto", "config", "middleware", "hook",
)
_IMPORT_PREFIXES = ("import ", "from ", "require(", "require (", "using ", "use ")

_FUNCTION_PATTERNS_BY_EXT: dict[str, tuple[re.Pattern[str], ...]] = {
    ".py": (
        re.compile(r"^\s*(?:async\s+)?def\s+\w+\s*\("),
        re.compile(r"^\s*class\s+\w+"),
    ),
    ".js": (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+\s*\("),
        re.compile(r"^\s*(?:export\s+)?class\s+\w+"),
    ),
    ".jsx": (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+\s*\("),
        re.compile(r"^\s*(?:export\s+)?class\s+\w+"),
    ),
    ".ts": (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+\s*\("),
        re.compile(r"^\s*(?:export\s+)?class\s+\w+"),
    ),
    ".tsx": (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+\s*\("),
        re.compile(r"^\s*(?:export\s+)?class\s+\w+"),
    ),
    ".vue": (
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+\w+\s*\("),
        re.compile(r"^\s*(?:export\s+)?class\s+\w+"),
    ),
    ".go": (re.compile(r"^\s*func\s+\w+\s*\("),),
    ".rs": (re.compile(r"^\s*fn\s+\w+\s*\("),),
    ".java": (
        re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\],\s]+\w+\s*\("),
        re.compile(r"^\s*(?:public|private)\s+class\s+\w+"),
    ),
    ".kt": (
        re.compile(r"^\s*fun\s+\w+\s*\("),
        re.compile(r"^\s*class\s+\w+"),
    ),
    ".cs": (
        re.compile(r"^\s*(?:public|private|protected|internal)\s+[\w<>\[\],\s]+\w+\s*\("),
        re.compile(r"^\s*(?:public|private)\s+class\s+\w+"),
    ),
    ".rb": (
        re.compile(r"^\s*(?:def|class)\s+\w+"),
    ),
    ".php": (re.compile(r"\bfunction\s+\w+\s*\("),),
}

_CODE_EVIDENCE_CHARS = 6_000  # per-file cap when embedding sampled source in the report
_CODE_EVIDENCE_TOTAL_CHARS = 120_000  # total cap for the sampled-source appendix

# Directories that never contain application code worth analyzing/exporting.
# The walk records them but does not descend (content API); recursive trees are
# filtered by path segment.
_NOISE_DIRS = (
    "test",
    "target",
    "build",
    "dist",
    "node_modules",
    ".git",
    ".mvn",
    "coverage",
    "__pycache__",
    ".idea",
    ".vscode",
)


@dataclass
class ResolvedSource:
    """A retrieval item resolved into exportable content or native bytes."""

    item: ExportContextItem
    source_type: str
    source_id: str
    source_name: str
    filename: str | None = None
    mime_type: str | None = None
    source_url: str | None = None
    native_bytes: bytes | None = None
    native_format: str | None = None
    content: str = ""
    structure: dict[str, Any] = field(default_factory=dict)
    analysis: ExportPayload | None = None
    sampled_sources: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None

    @property
    def has_native_bytes(self) -> bool:
        return self.native_bytes is not None


@dataclass
class ExportServices:
    """Optional service handles the resolver may use (additive; may be None)."""

    document_file_repo: DocumentFileRepository | None = None
    confluence_service: ConfluenceService | None = None
    github_service: GitHubService | None = None
    sharepoint_service: SharePointService | None = None


@dataclass
class ExportLimits:
    max_files: int = 100
    max_total_bytes: int = 100 * 1024 * 1024
    max_single_file_bytes: int = 25 * 1024 * 1024
    max_github_source_bytes: int = 25 * 1024 * 1024
    max_generated_report_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_settings(cls, settings: Any) -> ExportLimits:
        mb = 1024 * 1024
        return cls(
            max_files=settings.export_max_files,
            max_total_bytes=int(settings.export_max_total_size_mb * mb),
            max_single_file_bytes=int(settings.export_max_single_file_size_mb * mb),
            max_github_source_bytes=int(settings.export_max_github_source_size_mb * mb),
            max_generated_report_bytes=int(settings.export_max_generated_report_size_mb * mb),
        )


def resolve_sources(
    items: list[ExportContextItem],
    session_id: str,
    services: ExportServices | None = None,
    limits: ExportLimits | None = None,
) -> list[ResolvedSource]:
    """Resolve every item into a :class:`ResolvedSource` (best-effort, quoted)."""
    services = services or ExportServices()
    limits = limits or ExportLimits()
    resolved: list[ResolvedSource] = []
    for item in items:
        source = _resolve_item(item, session_id, services, limits)
        if source is not None:
            source.structure = detect_structure(source.content or "")
            resolved.append(source)
    return resolved


def _resolve_item(
    item: ExportContextItem,
    session_id: str,
    services: ExportServices,
    limits: ExportLimits,
) -> ResolvedSource | None:
    source_type = item.source_type or ""
    source_id = item.source_id or ""
    source_name = item.source_name or source_id
    filename = item.filename or (source_id if source_type == SourceType.UPLOADED_DOCUMENT.value else None)
    metadata = item.meta or {}
    captured = _captured_content(item)

    if source_type == SourceType.UPLOADED_DOCUMENT.value:
        return _resolve_upload(filename, source_id, source_name, item, session_id, services, captured)

    if source_type == SourceType.CONFLUENCE.value:
        return _resolve_confluence(source_id, source_name, item, services, captured)

    if source_type == SourceType.SHAREPOINT.value:
        return _resolve_sharepoint(source_id, source_name, item, services, captured, metadata)

    if source_type == SourceType.GITHUB.value:
        return _resolve_github(source_id, source_name, item, services, captured, limits)

    # Unknown source type: never export it.
    return None


def _captured_content(item: ExportContextItem) -> str:
    reference = item.content_reference or ""
    if not reference:
        return ""
    try:
        payload = json.loads(reference)
    except ValueError:
        return reference
    return str(payload.get("content") or "")


def _resolve_upload(
    filename: str | None,
    source_id: str,
    source_name: str,
    item: ExportContextItem,
    session_id: str,
    services: ExportServices,
    captured: str,
) -> ResolvedSource:
    native_bytes: bytes | None = None
    native_format: str | None = None
    error: str | None = None
    if services.document_file_repo is not None and filename:
        try:
            record = services.document_file_repo.find_by_session_and_filename(session_id, filename)
            if record is not None and record.content:
                native_bytes = record.content
                native_format = _extension_of(filename)
        except Exception:  # noqa: BLE001 - persistence failure degrades to generated
            logger.warning("Failed to read stored bytes for uploaded '%s':", filename)

    return ResolvedSource(
        item=item,
        source_type=SourceType.UPLOADED_DOCUMENT.value,
        source_id=source_id,
        source_name=source_name,
        filename=filename,
        mime_type=item.mime_type or _mime_of(filename),
        source_url=None,
        native_bytes=native_bytes,
        native_format=native_format,
        content=captured,
        error=error,
    )


def _resolve_confluence(
    source_id: str,
    source_name: str,
    item: ExportContextItem,
    services: ExportServices,
    captured: str,
) -> ResolvedSource:
    content = captured
    url = item.source_url
    error: str | None = None
    service = services.confluence_service
    if service is not None and service.enabled and source_id:
        try:
            text = service.get_page(source_id)
            parsed = parse_confluence_page(text)
            if parsed and parsed.get("content"):
                content = parsed["content"]
                if parsed.get("url"):
                    url = parsed.get("url") or url
                if parsed.get("title"):
                    source_name = parsed["title"]
        except Exception as exc:  # noqa: BLE001 - fall back to captured content
            logger.warning("Confluence enrichment failed for page %s: %s", source_id, exc)
            error = "Confluence enrichment unavailable; using retrieved content."
    return ResolvedSource(
        item=item,
        source_type=SourceType.CONFLUENCE.value,
        source_id=source_id,
        source_name=source_name,
        filename=None,
        mime_type=item.mime_type,
        source_url=url,
        content=content,
        error=error,
    )


def _resolve_sharepoint(
    source_id: str,
    source_name: str,
    item: ExportContextItem,
    services: ExportServices,
    captured: str,
    metadata: dict[str, Any],
) -> ResolvedSource:
    native_bytes: bytes | None = None
    native_format: str | None = None
    mime = item.mime_type
    url = item.source_url
    error: str | None = None
    service = services.sharepoint_service
    drive_id = str(metadata.get("drive_id") or "")
    document_id = str(metadata.get("document_id") or source_id)
    if service is not None and service.enabled and drive_id:
        try:
            data, meta = service.download_document_bytes(document_id=document_id, drive_id=drive_id)
            native_bytes = data
            name = str(meta.get("name") or source_name)
            native_format = _extension_of(name)
            mime = str(meta.get("mime_type") or mime or _mime_of(name))
            if meta.get("web_url"):
                url = meta.get("web_url") or url
            if name:
                source_name = name
        except Exception as exc:  # noqa: BLE001 - degrade to captured content
            logger.warning("SharePoint native download failed for %s: %s", source_id, exc)
            error = f"Native download unavailable ({exc}); generated from retrieved content."
    return ResolvedSource(
        item=item,
        source_type=SourceType.SHAREPOINT.value,
        source_id=source_id,
        source_name=source_name,
        filename=str(metadata.get("filename") or source_name),
        mime_type=mime,
        source_url=url,
        native_bytes=native_bytes,
        native_format=native_format,
        content=captured,
        error=error,
    )


def _resolve_github(
    source_id: str,
    source_name: str,
    item: ExportContextItem,
    services: ExportServices,
    captured: str,
    limits: ExportLimits,
) -> ResolvedSource:
    service = services.github_service
    if service is not None and service.enabled:
        try:
            analysis, sampled = build_github_analysis(source_id, service, limits=limits)
            return ResolvedSource(
                item=item,
                source_type=SourceType.GITHUB.value,
                source_id=source_id,
                source_name=source_name,
                filename=None,
                mime_type=None,
                source_url=f"https://github.com/{source_id}".replace("https://github.com//", "https://github.com/"),
                native_bytes=None,
                content=captured,
                analysis=analysis,
                sampled_sources=sampled,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to a captured analysis
            logger.warning("GitHub analysis failed for %s: %s", source_id, exc)
            return ResolvedSource(
                item=item,
                source_type=SourceType.GITHUB.value,
                source_id=source_id,
                source_name=source_name,
                filename=None,
                source_url=item.source_url,
                content=captured,
                analysis=_captured_only_analysis(source_id, captured),
                error=f"Repository re-analysis unavailable ({exc}); using retrieved context.",
            )
    return ResolvedSource(
        item=item,
        source_type=SourceType.GITHUB.value,
        source_id=source_id,
        source_name=source_name,
        filename=None,
        source_url=item.source_url,
        content=captured,
        analysis=_captured_only_analysis(source_id, captured),
        error="GitHub service unavailable; using retrieved context.",
    )


def _captured_only_analysis(repo: str, captured: str) -> ExportPayload:
    payload = ExportPayload(title=f"{repo} - Repository Analysis", source_type=SourceType.GITHUB.value)
    if captured:
        payload.sections.append(
            ExportSection(
                heading="Retrieved Context",
                body=captured.strip(),
                evidence="Observed from retrieval context",
            )
        )
    payload.sections.append(
        ExportSection(
            heading="Repository Information",
            body="The repository could not be re-analyzed from GitHub in this export.",
            evidence="Unavailable",
        )
    )
    return payload


def detect_structure(text: str) -> dict[str, Any]:
    """Detect the structural kind of captured/enriched content.

    Returns ``{"kind": <one of json|html|markdown|tabular|prose>, "rows": [...],
    "headers": [...]}``. Used by the deterministic intent proposer so a generated
    document's format follows its content (never a blanket developer preference).
    """
    result: dict[str, Any] = {"kind": "prose", "rows": [], "headers": []}
    stripped = (text or "").strip()
    if not stripped:
        return result
    head = stripped[:2000].lower()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(stripped)
            result["kind"] = "json"
            return result
        except ValueError:
            pass
    if re.search(r"<(?:html|h1|h2|h3|p|table)[\s>]", head):
        result["kind"] = "html"
        return result
    if re.search(r"(?m)^#{1,6}\s", stripped):
        result["kind"] = "markdown"
        return result
    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) >= 3:
        for delimiter in ("\t", "|", ","):
            counts = [line.count(delimiter) for line in lines]
            if counts and counts[0] >= 1 and max(counts) == min(counts):
                rows = [[cell.strip() for cell in line.split(delimiter)] for line in lines]
                if all(len(row) == len(rows[0]) and len(row) >= 2 for row in rows):
                    result["kind"] = "tabular"
                    result["headers"] = rows[0]
                    result["rows"] = rows[1:]
                    return result
    return result


# --------------------------------------------------------------------------
# GitHub repository analysis (deterministic, evidence-driven).
# --------------------------------------------------------------------------


def build_github_analysis(
    repo: str,
    service: GitHubService,
    *,
    limits: ExportLimits | None = None,
    max_code_files: int = 25,
) -> tuple[ExportPayload, list[tuple[str, str]]]:
    limits = limits or ExportLimits()
    payload = ExportPayload(
        title=f"{repo} - Repository Analysis",
        source_type=SourceType.GITHUB.value,
        source_name=repo,
    )
    sampled: list[tuple[str, str]] = []
    collected_bytes = 0
    collected: dict[str, str] = {}

    def read_into(path: str, text: str) -> None:
        nonlocal collected_bytes
        if len(text) > limits.max_single_file_bytes:
            text = text[: limits.max_single_file_bytes]
        collected[path] = text
        collected_bytes += len(text)

    repo_meta = ""
    try:
        repo_text = service.get_repository(repo)
        meta_lines = [
            line for line in repo_text.splitlines() if not line.startswith("[Source:")
        ]
        repo_meta = "\n".join(meta_lines).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("GitHub repository metadata unavailable for %s: %s", repo, exc)

    readme = ""
    try:
        readme_block = service.get_readme(repo)
        lines = readme_block.splitlines()
        while lines and lines[0].startswith("[Source:"):
            lines.pop(0)
        if lines and lines[0].startswith("README of"):
            lines.pop(0)
        readme = "\n".join(lines).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("README unavailable for %s: %s", repo, exc)

    tree: list[dict[str, Any]] = []
    try:
        # Deep, still-bounded walk: the recursive git-trees endpoint returns the whole
        # tree in a single request; the contents API fallback never descends into
        # test/build noise, so microservice sources are reached cheaply.
        tree = service.walk_repository(
            repo,
            max_items=min(limits.max_files * 4, 500),
            max_depth=8,
            skip_dirs=_NOISE_DIRS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Repository walk unavailable for %s: %s", repo, exc)

    file_paths = [entry["path"] for entry in tree if entry.get("type") == "file"]

    # Build/dependency files
    build_files: list[str] = [
        path
        for path in file_paths
        if not _is_noise(path)
        and (
            path.rsplit("/", 1)[-1].lower() in _KEY_BUILD_FILES
            or path.lower().endswith((".gradle", "pom.xml"))
        )
    ]

    code_files: list[str] = []
    for path in file_paths:
        if _is_noise(path):
            continue
        if path.lower().endswith(_CODE_EXTENSIONS):
            code_files.append(path)
        if len(code_files) >= max_code_files * 3:
            break

    # Read build files then code file samples (bounded total). Code files are
    # read in priority order (entry points and core layer files first) so the
    # analysis is built from where the application really starts.
    for path in build_files[: max(max_code_files // 2, 5)]:
        if collected_bytes >= limits.max_github_source_bytes:
            break
        if _is_sensitive(path):
            continue
        try:
            text = service.get_file_content_text(repo, path, char_limit=limits.max_single_file_bytes)
            if text and "\x00" not in text:
                read_into(path, text)
        except Exception:  # noqa: BLE001 - a missing file is not fatal
            continue

    for path in sorted(code_files, key=_code_read_key)[:max_code_files]:
        if collected_bytes >= limits.max_github_source_bytes:
            break
        if _is_sensitive(path):
            continue
        try:
            text = service.get_file_content_text(repo, path, char_limit=limits.max_single_file_bytes)
            if text and "\x00" not in text:
                read_into(path, text)
        except Exception:  # noqa: BLE001
            continue

    # ---- Build the report sections --------------------------------------
    payload.sections.append(
        ExportSection(
            heading="Repository Information",
            body=repo_meta or "Repository metadata could not be retrieved.",
            evidence="Observed from source",
        )
    )
    payload.sections.append(
        ExportSection(
            heading="Executive Summary",
            body=_first_chars(readme or repo_meta, 1200) or "No README or description was retrieved.",
            evidence="Observed from source" if (readme or repo_meta) else "Unavailable",
        )
    )

    structure = "\n".join(
        f"{'directory' if entry.get('type') == 'dir' else 'file'} {entry.get('path') or '/'}"
        for entry in tree[:200]
    )
    structure_rows = [
        ["directory" if entry.get("type") == "dir" else "file", str(entry.get("path") or "/")]
        for entry in tree[:200]
    ]
    payload.sections.append(
        ExportSection(
            heading="Repository Structure",
            body=structure or "Repository structure could not be retrieved.",
            evidence="Observed from source",
            headers=["Type", "Path"],
            rows=structure_rows,
        )
    )

    modules = _module_dirs(
        sorted({entry.get("path", "") for entry in tree if entry.get("type") != "file"})
    )
    payload.sections.append(
        ExportSection(
            heading="Modules / Services",
            body=("\n".join(f"- {module}" for module in modules) or "No module directories observed.")
            + "\n\nTop-level entry points and source files are listed under Important Code Paths.",
            evidence="Inferred from repository structure",
        )
    )

    payload.sections.append(
        ExportSection(
            heading="Entry Points & Code Flow",
            body=_extract_code_flow(collected),
            evidence="Observed from source",
        )
    )

    functions = _extract_functions(collected)
    payload.sections.append(
        ExportSection(
            heading="Functions & Classes (sampled)",
            body="\n".join(functions) if functions else
            "No functions or classes were observed in the sampled source files.",
            evidence="Observed from source" if functions else "Unavailable",
            headers=["File:Line", "Signature"],
            rows=_signature_rows(functions),
        )
    )

    endpoints = _extract_endpoints(repo, collected)
    payload.sections.append(
        ExportSection(
            heading="API Endpoints",
            body=endpoints or "No route definitions were observed in the sampled source files.",
            evidence="Observed from source" if endpoints else "Unavailable",
            headers=["File", "Definition"],
            rows=_colon_rows(endpoints),
        )
    )

    layers = _collect_architecture_layers(collected)
    payload.sections.append(
        ExportSection(
            heading="System Architecture",
            body=render_architecture_text(layers),
            evidence="Inferred from sampled source files",
        )
    )
    payload.diagram_png = _architecture_png(layers)

    deps = _extract_dependencies(repo, collected)
    payload.sections.append(
        ExportSection(
            heading="Dependencies & Technology Stack",
            body=deps or "No dependency manifests were observed in the sampled files.",
            evidence="Observed from source" if deps else "Unavailable",
            headers=["Manifest", "Dependencies"],
            rows=_dependency_rows(deps),
        )
    )

    api_contracts = _extract_api_contracts(collected)
    payload.sections.append(
        ExportSection(
            heading="HTTP API Contract Details",
            body="\n".join(" | ".join(row) for row in api_contracts)
            or "No endpoint contracts were observed in the sampled files.",
            evidence="Observed from source" if api_contracts else "Unavailable",
            headers=["Method", "Path", "Source", "Parameters"],
            rows=api_contracts,
        )
    )

    data_models = _extract_data_models(collected)
    payload.sections.append(
        ExportSection(
            heading="Data Models & Schema",
            body="\n".join(data_models)
            or "No data model or schema definitions were observed in the sampled files.",
            evidence="Observed from source" if data_models else "Unavailable",
        )
    )

    service_methods = _extract_service_methods(collected)
    payload.sections.append(
        ExportSection(
            heading="Service Layer & Business Logic Resources",
            body="\n".join(service_methods)
            or "No service-layer classes were observed in the sampled files.",
            evidence="Observed from source" if service_methods else "Unavailable",
        )
    )

    integrations = _extract_external_integrations(collected)
    payload.sections.append(
        ExportSection(
            heading="External Integrations",
            body="\n".join(integrations)
            or "No external integrations were observed in the sampled files.",
            evidence="Observed from source" if integrations else "Unavailable",
        )
    )

    config_summary = _extract_config_summary(collected)
    payload.sections.append(
        ExportSection(
            heading="Configuration Summary",
            body="\n".join(config_summary)
            or "No configuration values were observed in the sampled files.",
            evidence="Observed from source" if config_summary else "Unavailable",
        )
    )

    payload.sections.append(
        ExportSection(
            heading="Build & Configuration",
            body=_build_configuration(repo, collected)
            or "No build/configuration files were retrieved (or they are absent).",
            evidence="Observed from source",
        )
    )

    code_snapshot = []
    for path in list(collected):
        if path.lower().endswith(_CODE_EXTENSIONS):
            code_snapshot.append(f"{path} ({_first_chars(collected[path], 200).strip() or 'binary/large'})")
    payload.sections.append(
        ExportSection(
            heading="Important Code Paths",
            body=("\n".join(code_snapshot) if code_snapshot else "No source files were sampled.")
            + "\n\nFull sampled file contents are included under source/ when available.",
            evidence="Observed from source" if code_snapshot else "Unavailable",
        )
    )

    findings: list[str] = []
    if not readme:
        findings.append("No README was retrieved; the overview may be incomplete.")
    if not endpoints:
        findings.append("No framework route patterns were observed in sampled files.")
    skipped = [path for path in file_paths if _is_sensitive(path)]
    if skipped:
        findings.append(
            f"{len(skipped)} sensitive file(s) (.env*, credentials, keys) were detected and "
            "excluded from this export."
        )
    if collected_bytes >= limits.max_github_source_bytes:
        findings.append("The GitHub source sampling size limit was reached; later files were not read.")
    findings.append("This analysis is evidence-based; anything not observed is marked Unavailable.")
    payload.sections.append(
        ExportSection(
            heading="Key Findings & Caveats",
            body="\n".join(f"- {finding}" for finding in findings) if findings else
            "No caveats recorded.",
            evidence="Observed from source / Generated analysis",
        )
    )

    payload.sections.append(
        ExportSection(
            heading="Sources",
            body=("\n".join(f"- {path}" for path in sorted(collected)) or "No files were read."),
            evidence="Observed from source",
        )
    )

    # Append the bounded sampled-source appendix so the report and the optional LLM
    # narrative are grounded in the actual code (never credentials: sensitive files
    # were already excluded while reading).
    appendix_chars = 0
    for path in sorted(collected, key=_code_read_key):
        if not path.lower().endswith(_CODE_EXTENSIONS):
            continue
        if appendix_chars >= _CODE_EVIDENCE_TOTAL_CHARS:
            break
        body = _first_chars(collected[path], _CODE_EVIDENCE_CHARS)
        appendix_chars += len(body)
        payload.sections.append(
            ExportSection(
                heading=f"File: {path}",
                body=body,
                evidence="Observed from source",
            )
        )

    # Sampled source files (bounded) for archive ``source/`` folders.
    sampled = [(path, text) for path, text in sorted(collected.items()) if path.lower().endswith(_CODE_EXTENSIONS)]
    for path in build_files[:3]:
        if path in collected and (path, collected[path]) not in sampled:
            sampled.append((path, collected[path]))
    return payload, sampled


def _first_chars(text: str, limit: int) -> str:
    return (text or "").strip()[:limit]


def _is_sensitive(path: str) -> bool:
    lower = ("/" + (path or "").lstrip("/")).lower()
    return any(marker.lower() in lower for marker in _SENSITIVE_MARKERS)


def _is_noise(path: str) -> bool:
    segments = (path or "").lower().split("/")
    return any(segment in _NOISE_DIRS for segment in segments)


def _module_dirs(paths: list[str]) -> list[str]:
    modules: list[str] = []
    for path in paths:
        parts = path.split("/")
        if not parts or parts[0] in {"", "."}:
            continue
        if parts[0].startswith("src"):
            top = "/".join(parts[:3]) if len(parts) >= 3 and parts[1] in {"main", "test", "app"} else "/".join(parts[:2])
        else:
            top = parts[0]
        if top and top not in modules:
            modules.append(top)
    suggested = [
        "src/main", "src/test", "src/app", "lib", "packages", "services",
        "api", "models", "controllers", "routes", "modules", "common",
        "core", "config", "client", "web", "worker", "backend", "frontend",
    ]
    ordered = [m for m in suggested if m in modules] + [m for m in modules if m not in suggested]
    return ordered[:40]


def _code_read_key(path: str) -> tuple[int, int, str]:
    """Sort key that reads entry-point and core-layer files before the rest."""
    if _is_entry_named(path):
        rank = 0
    elif any(hint in path.lower() for hint in _LAYER_HINTS):
        rank = 1
    else:
        rank = 2
    return (rank, path.count("/"), path)


def _is_entry_named(path: str) -> bool:
    base = (path.rsplit("/", 1)[-1] or "").lower()
    return (
        base in _ENTRY_BASENAMES
        or base.startswith("__main__")
        or base.endswith("application.java")
    )


def _base_no_ext(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def _ext_of(path: str) -> str:
    if "." not in path:
        return ""
    return "." + path.rsplit(".", 1)[-1].lower()


def _is_entry_file(path: str, text: str | None) -> bool:
    if _is_entry_named(path):
        return True
    if not text:
        return False
    head = text[:4000]
    return any(marker in head for marker in _ENTRY_CONTENT_MARKERS)


def _extract_code_flow(files: dict[str, str]) -> str:
    """Entry points, top-level flow lines and import edges between sampled files."""
    entry_files = [path for path in files if _is_entry_file(path, files[path])]
    entry_files.sort(key=lambda p: (p.count("/"), p))
    roots = {_base_no_ext(path) for path in files if _base_no_ext(path)}

    edges: list[str] = []
    for path in sorted(files):
        if not path.lower().endswith(_CODE_EXTENSIONS):
            continue
        base = _base_no_ext(path)
        seen_for_file = False
        for line in (files[path] or "")[:100_000].splitlines():
            stripped = line.strip()
            lowered = stripped.lower()
            if not lowered.startswith(_IMPORT_PREFIXES):
                continue
            tokens = re.findall(r"[A-Za-z_]\w*", stripped)
            for token in tokens:
                root = token.strip("'\"/")
                if root in roots and root != base:
                    edges.append(f"- {path} -> {root}  ({stripped[:100]})")
                    seen_for_file = True
                    break
            if seen_for_file:
                break
        if len(edges) >= 60:
            break

    parts: list[str] = []
    if entry_files:
        parts.append("Entry Points\n" + "\n".join(f"- {path}" for path in entry_files))
    else:
        parts.append("Entry Points\n- No clear entry-point file was observed in the sampled files.")
    if entry_files:
        flow_lines: list[str] = []
        for path in entry_files[:5]:
            head = [ln.strip() for ln in (files.get(path) or "").splitlines()[:400] if ln.strip()][:6]
            flow_lines.append(f"- {path}: " + " | ".join(head))
        parts.append("Top-level Flow (entry files)\n" + "\n".join(flow_lines))
    if edges:
        parts.append("Import / Reference Edges (sampled)\n" + "\n".join(edges))
    else:
        parts.append(
            "Import / Reference Edges (sampled)\n"
            "- No direct import edges between the sampled files were observed."
        )
    parts.append(
        "These are the file-level relationships observed in the sampled sources; the "
        "narrative Code Flow section (when enabled) traces the runtime call chains."
    )
    return "\n\n".join(parts)


def _extract_functions(files: dict[str, str]) -> list[str]:
    """Line-numbered signature inventory of functions/classes in sampled code."""
    inventory: list[str] = []
    for path in sorted(files, key=_code_read_key):
        patterns = _FUNCTION_PATTERNS_BY_EXT.get(_ext_of(path))
        if not patterns:
            continue
        for index, line in enumerate((files[path] or "")[:100_000].splitlines(), start=1):
            if index > 3000:
                break
            for pattern in patterns:
                if pattern.search(line):
                    inventory.append(f"{path}:{index}: {line.strip()[:120]}")
                    break
        if len(inventory) >= 90:
            break
    return inventory


def _extract_endpoints(repo: str, files: dict[str, str]) -> str:
    results: list[str] = []
    for path, text in files.items():
        for pattern in _ROUTE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            segment = text.strip()
            start = max(0, match.start() - 40)
            snippet = " ".join(segment[start : match.end() + 40].split())
            if not _is_sensitive(path):
                results.append(f"{path}: {snippet}")
            break
        if len(results) >= 40:
            break
    return "\n".join(results[:40])


# Architecture-diagram classification. Pair of (layer title, path keywords); the
# first rule whose keyword appears in the lowercased path wins after the entry-point
# check. Component names shown in the diagram drop these trailing role suffixes.
_ARCH_LAYER_RULES = (
    ("API / REST Layer", ("controller", "api", "resource", "rest", "endpoints", "routes", "router", "handler", "websocket")),
    ("Business Logic / Services", ("service", "usecase", "use_case", "business", "facade", "manager", "core", "domain")),
    ("Data Access", ("repositor", "dao", "dataaccess", "data_access", "dal", "mapper", "persistence", "store")),
    ("Data Models", ("model", "entity", "schema", "dto")),
    ("Config & Infrastructure", ("config", "configuration", "infra", "infrastructure", "settings", "middleware", "util", "common")),
)

_ARCH_STEM_SUFFIXES = (
    "controller", "controllers", "service", "services", "repository", "repositories",
    "router", "routes", "handler", "manager", "resource", "mapper", "dao", "dto",
    "model", "models", "entity", "entities", "schema", "config", "configuration",
    "gateway", "application", "helper", "util", "client",
)


def _first_identifier(text: str | None) -> str | None:
    """First declared type/function name in sampled source, if any."""
    patterns = (
        re.compile(r"\b(?:public|protected|private|abstract|final|static)?\s*class\s+([A-Za-z_]\w*)"),
        re.compile(r"^\s*(?:export\s+default\s+)?class\s+([A-Za-z_]\w*)"),
        re.compile(r"^\s*(?:class|def)\s+([A-Za-z_]\w*)"),
        re.compile(r"(?:^|\n)\s*(?:def|func|fn)\s+([A-Za-z_]\w*)\s*[\(:]"),
    )
    for pattern in patterns:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return None


def _culture_component_name(path: str) -> str:
    """Human label for a file in the diagram (stem minus role suffix)."""
    stem = _base_no_ext(path) or path
    lower = stem.lower()
    for suffix in _ARCH_STEM_SUFFIXES:
        if lower.endswith(suffix) and len(stem) > len(suffix) + 1:
            stem = stem[: -len(suffix)]
            break
    return stem or _base_no_ext(path) or path


def _collect_architecture_layers(collected: dict[str, str]) -> list[ArchitectureLayer]:
    """Classify sampled code files into top-down architecture layers."""
    layers = [ArchitectureLayer(title, []) for title in LAYER_TITLES]
    by_title = {layer.title: layer for layer in layers}
    for path in sorted(collected, key=_code_read_key):
        if _is_noise(path) or not path.lower().endswith(_CODE_EXTENSIONS):
            continue
        if _is_entry_named(path):
            target = by_title["Entry Point"]
        else:
            target = None
            for title, hints in _ARCH_LAYER_RULES:
                if any(hint in path.lower() for hint in hints):
                    target = by_title[title]
                    break
            if target is None:
                continue
        assert target is not None
        label = _first_identifier(collected.get(path)) or _culture_component_name(path)
        if len(target.items) < 6 and label not in target.items:
            target.items.append(label)
    return layers


def _architecture_png(layers: list[ArchitectureLayer]) -> bytes | None:
    """Best-effort PNG rendering of the diagram (``None`` when unavailable)."""
    try:
        from app.export.diagrams import render_architecture_png as _render

        return _render(layers)
    except Exception:  # noqa: BLE001 - the ASCII diagram remains the fallback
        return None


def _signature_rows(inventory: list[str]) -> list[list[str]]:
    """Convert ``path:line: signature`` lines into (file:line, signature) table rows."""
    rows: list[list[str]] = []
    for line in inventory:
        match = re.match(r"^(.*?):(\d+):\s*(.*)$", line)
        if match:
            rows.append([f"{match.group(1)}:{match.group(2)}", match.group(3)])
        elif line.strip():
            rows.append(["", line.strip()])
    return rows


def _colon_rows(text: str) -> list[list[str]]:
    """Split ``path: value`` lines into two columns at the first ``: ``."""
    rows: list[list[str]] = []
    for line in (text or "").splitlines():
        if ": " in line:
            key, _, value = line.partition(": ")
            rows.append([key.strip(), value.strip()])
        elif line.strip():
            rows.append(["", line.strip()])
    return rows


def _dependency_rows(text: str) -> list[list[str]]:
    """Group dependency-manifest blocks into (manifest, dependencies) rows."""
    rows: list[list[str]] = []
    for block in (text or "").split("\n\n"):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        first = lines[0]
        if ":" in first and not first.startswith(("-", "*")):
            label, _, rest = first.partition(":")
            values = [rest.strip(), *(line for line in lines[1:] if not line.startswith(("-", "*")))]
            rows.append([label, ", ".join(v for v in values if v)])
        else:
            rows.append(["", ", ".join(lines)])
    return rows


def _extract_data_models(collected: dict[str, str]) -> list[str]:
    """Extract data model / entity definitions from source files.

    Targets:
    - Java: @Entity, @Table, @Document classes with field declarations
    - Python: SQLAlchemy models (class ...Base), Pydantic schemas (class ...BaseModel)
    """
    models: list[str] = []
    # Java/Spring entity patterns
    java_entity = re.compile(r"@Entity\b.*?class\s+(\w+)", re.DOTALL)
    java_field = re.compile(r"private\s+([\w<>\[\].]+)\s+(\w+)\s*[;=]")
    java_column = re.compile(r"@Column(?:\((?:name\s*=\s*\"([^\"]+)\"[^)]*)?\))?")

    # Python/SQLAlchemy patterns
    py_model = re.compile(r"class\s+(\w+)\s*\(\s*(?:app\.models\.)?Base\b")
    py_field = re.compile(r"(\w+)\s*[:=]\s*mapped_column\(\s*([\w\[\]|.]+)")
    py_tablename = re.compile(r"__tablename__\s*=\s*['\"]([^'\"]+)['\"]")

    # Python/Pydantic patterns
    py_schema = re.compile(r"class\s+(\w+)\s*\(\s*(?:pydantic\.)?BaseModel\b")
    py_schema_field = re.compile(r"(\w+)\s*:\s*([\w\[\], |.]+)")

    for path in sorted(collected, key=_code_read_key):
        if not path.lower().endswith(_CODE_EXTENSIONS):
            continue
        text = collected[path]
        if not text:
            continue

        # Java entities
        if path.lower().endswith(".java"):
            for match in java_entity.finditer(text):
                entity_name = match.group(1)
                fields: list[str] = []
                # Extract fields in the class body (simplified: next 50 lines)
                body_start = match.end()
                body_lines = text[body_start:body_start + 2000].splitlines()
                for line in body_lines[:50]:
                    if line.strip().startswith("}"):
                        break
                    if "@Id" in line and not re.search(r"private\s+", line):
                        continue
                    field_match = java_field.search(line)
                    if field_match:
                        field_type, field_name = field_match.groups()
                        col_match = java_column.search(line)
                        col_name = col_match.group(1) if col_match and col_match.group(1) else field_name
                        fields.append(f"  {field_name}: {field_type} (column: {col_name})")
                if fields:
                    models.append(f"{path}: {entity_name}")
                    models.extend(fields[:10])  # cap at 10 fields per entity

        # Python models/schemas
        elif path.lower().endswith(".py"):
            for match in py_model.finditer(text):
                model_name = match.group(1)
                tablename_match = py_tablename.search(text[match.end():match.end() + 500])
                tablename = tablename_match.group(1) if tablename_match else "unknown"
                fields = []
                body_start = match.end()
                body_lines = text[body_start:body_start + 1500].splitlines()
                for line in body_lines[:40]:
                    if line.strip() and not line.strip().startswith("#"):
                        field_match = py_field.search(line)
                        if field_match:
                            field_name, field_type = field_match.groups()
                            fields.append(f"  {field_name}: {field_type}")
                if fields:
                    models.append(f"{path}: {model_name} (table: {tablename})")
                    models.extend(fields[:10])

            # Pydantic schemas
            for match in py_schema.finditer(text):
                schema_name = match.group(1)
                fields = []
                body_start = match.end()
                body_lines = text[body_start:body_start + 1000].splitlines()
                for line in body_lines[:30]:
                    if line.strip() and not line.strip().startswith("#"):
                        field_match = py_schema_field.search(line)
                        if field_match:
                            field_name, field_type = field_match.groups()
                            fields.append(f"  {field_name}: {field_type}")
                if fields:
                    models.append(f"{path}: {schema_name} (Pydantic schema)")
                    models.extend(fields[:8])

    return models[:60]  # cap total


def _extract_service_methods(collected: dict[str, str]) -> list[str]:
    """Extract service layer methods and their purposes.

    Targets:
    - Java: @Service classes with public methods
    - Python: Service classes with method definitions
    """
    services: list[str] = []
    # Java service patterns
    java_service = re.compile(r"@Service\b.*?class\s+(\w+)", re.DOTALL)
    java_method = re.compile(r"public\s+([\w<>\[\].]+)\s+(\w+)\s*\(([^)]*)\)")

    # Python service patterns (class with methods)
    py_class = re.compile(r"class\s+(\w+(?:Service|Repository|Manager))\b")
    py_method = re.compile(r"def\s+(\w+)\s*\((?:self|cls)")

    for path in sorted(collected, key=_code_read_key):
        if not path.lower().endswith(_CODE_EXTENSIONS):
            continue
        text = collected[path]
        if not text:
            continue

        # Java services
        if path.lower().endswith(".java"):
            for match in java_service.finditer(text):
                service_name = match.group(1)
                methods = []
                body_start = match.end()
                body_lines = text[body_start:body_start + 3000].splitlines()
                for line in body_lines[:60]:
                    if line.strip().startswith("}"):
                        break
                    method_match = java_method.search(line)
                    if method_match:
                        ret_type, method_name, params = method_match.groups()
                        # Skip constructors and common boilerplate
                        if method_name == service_name or method_name in ("toString", "hashCode", "equals"):
                            continue
                        methods.append(f"  {method_name}({params.strip()[:50]}): {ret_type}")
                if methods:
                    services.append(f"{path}: {service_name}")
                    services.extend(methods[:8])  # cap methods per service

        # Python services
        elif path.lower().endswith(".py"):
            for match in py_class.finditer(text):
                class_name = match.group(1)
                methods = []
                body_start = match.end()
                body_lines = text[body_start:body_start + 2000].splitlines()
                for line in body_lines[:50]:
                    method_match = py_method.search(line)
                    if method_match:
                        method_name = method_match.group(1)
                        if method_name.startswith("_"):  # skip private
                            continue
                        methods.append(f"  {method_name}()")
                if methods:
                    services.append(f"{path}: {class_name}")
                    services.extend(methods[:8])

    return services[:50]  # cap total


def _extract_api_contracts(collected: dict[str, str]) -> list[list[str]]:
    """Extract HTTP API contracts with method, path, and parameters.

    Returns rows of [Method, Path, Source, Parameters].
    """
    rows: list[list[str]] = []

    # Java/Spring mapping patterns with value parameter
    java_mapping = re.compile(
        r"@(?:(Get|Post|Put|Delete|Patch|Request)Mapping)"
        r"\((?:value\s*=\s*)?\"([^\"]+)\""
    )
    java_param = re.compile(r"@(?:(RequestBody|RequestParam|PathVariable|RequestHeader))\b(?:\([^)]*\))?\s*(?:final\s+)?[\w<>\[\].]+ (\w+)")

    # Python/FastAPI patterns
    py_route = re.compile(r"@router\.(get|post|put|delete|patch)\((?:\"([^\"]+)\"|'([^']+)')")

    for path in sorted(collected, key=_code_read_key):
        if not path.lower().endswith(_CODE_EXTENSIONS):
            continue
        text = collected[path]
        if not text:
            continue

        # Java endpoints
        if path.lower().endswith(".java"):
            for match in java_mapping.finditer(text):
                verb = match.group(1).upper() if match.group(1) != "Request" else "ANY"
                route = match.group(2)
                # Find parameters in the method after this annotation
                method_start = match.end()
                method_text = text[method_start:method_start + 500]
                params = []
                for param_match in java_param.finditer(method_text[:300]):
                    param_type, param_name = param_match.groups()
                    params.append(f"{param_type}:{param_name}")
                rows.append([verb, route, path, ", ".join(params[:3])])

        # Python endpoints
        elif path.lower().endswith(".py"):
            for match in py_route.finditer(text):
                verb = match.group(1).upper()
                route = match.group(2) or match.group(3)
                # Find function parameters
                func_start = match.end()
                func_text = text[func_start:func_start + 300]
                func_match = re.search(r"def\s+\w+\(([^)]*)\)", func_text)
                params = []
                if func_match:
                    for param in func_match.group(1).split(","):
                        param = param.strip()
                        if param and param != "request" and "=" not in param:
                            params.append(param.split(":")[0].strip())
                rows.append([verb, route, path, ", ".join(params[:3])])

    return rows[:40]  # cap


def _extract_external_integrations(collected: dict[str, str]) -> list[str]:
    """Identify external service integrations (APIs, databases, message queues).

    Looks for:
    - HTTP client calls (RestTemplate, WebClient, httpx, requests)
    - Database connections
    - Message queue producers/consumers
    - External base URLs
    """
    integrations: list[str] = []
    # External URL patterns
    url_pattern = re.compile(r"https?://[a-zA-Z0-9.-]+(?:\.[a-zA-Z]{2,})(?:/[^\"'\s>]*)?")

    # HTTP client patterns
    http_client = re.compile(r"(?:RestTemplate|WebClient|HttpClient|httpx|requests)\b")

    # Database patterns
    db_pattern = re.compile(r"(?:@Repository|JpaRepository|Session|engine|create_engine|sqlite|postgresql|mysql)")

    seen_urls: set[str] = set()

    for path in sorted(collected, key=_code_read_key):
        if not path.lower().endswith(_CODE_EXTENSIONS):
            continue
        text = collected[path]
        if not text:
            continue
        rel = path.rstrip("/")

        # Extract external URLs
        for url_match in url_pattern.finditer(text):
            url = url_match.group(0).rstrip("/").rstrip("\"'")
            # Skip internal/dev URLs
            if any(skip in url.lower() for skip in ["localhost", "127.0.0.1", "0.0.0.0", "example.com", "placeholder"]):
                continue
            if url not in seen_urls:
                seen_urls.add(url)
                integrations.append(f"External URL: {url} (referenced in {rel})")

        # HTTP clients
        if http_client.search(text):
            integrations.append(f"HTTP client usage in {rel}")

        # Database
        if db_pattern.search(text):
            integrations.append(f"Database interaction in {rel}")

    return integrations[:20]  # cap


def _extract_config_summary(collected: dict[str, str]) -> list[str]:
    """Extract key configuration values and feature flags.

    Targets:
    - application.yml/properties: key=value pairs
    - .env files: KEY=value
    - config.py: Settings class fields
    """
    config: list[str] = []

    for path in sorted(collected, key=_code_read_key):
        text = collected[path]
        if not text:
            continue
        lower = path.lower()

        # YAML config
        if lower.endswith((".yml", ".yaml")) and "application" in lower:
            lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
            for line in lines[:30]:
                if ":" in line:
                    key, _, value = line.partition(":")
                    if value.strip():
                        config.append(f"{path}: {key.strip()} = {value.strip()[:60]}")

        # Properties config
        elif lower.endswith(".properties"):
            lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
            for line in lines[:30]:
                if "=" in line:
                    key, _, value = line.partition("=")
                    config.append(f"{path}: {key.strip()} = {value.strip()[:60]}")

        # Python config/settings
        elif lower.endswith(".py") and ("config" in lower or "settings" in lower):
            # Look for class Settings or similar
            settings_class = re.search(r"class\s+(\w*(?:Config|Settings)\w*)\b", text)
            if settings_class:
                class_name = settings_class.group(1)
                fields = []
                body_start = settings_class.end()
                body_lines = text[body_start:body_start + 1000].splitlines()
                for line in body_lines[:20]:
                    if line.strip().startswith("class ") or line.strip().startswith("def "):
                        break
                    field_match = re.search(r"(\w+)\s*[:=]\s*(.+)", line)
                    if field_match:
                        field_name, field_value = field_match.groups()
                        if not field_name.startswith("_"):
                            fields.append(f"  {field_name} = {field_value[:40]}")
                if fields:
                    config.append(f"{path}: {class_name}")
                    config.extend(fields[:10])

    return config[:30]  # cap


def _extract_dependencies(repo: str, files: dict[str, str]) -> str:
    blocks: list[str] = []
    for path in sorted(files):
        lower = path.lower()
        text = files[path]
        if lower.endswith("package.json"):
            try:
                data = json.loads(text)
            except ValueError:
                data = {}
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
            if deps:
                blocks.append(f"{path}: " + ", ".join(sorted(deps)[:60]))
        elif lower.endswith("requirements.txt"):
            blocks.append(f"{path}:\n" + "\n".join(text.splitlines()[:60]))
        elif lower.endswith("pom.xml"):
            artifacts = _parse_pom_dependencies(text)
            blocks.append(
                f"{path}: " + ", ".join(artifacts[:60]) if artifacts else f"{path}: (no artifacts parsed)"
            )
        elif lower.endswith(("build.gradle", "build.gradle.kts", "settings.gradle")):
            picks = [
                line.strip()
                for line in text.splitlines()
                if line.strip().startswith(("implementation ", "api ", "compile ", "runtimeOnly ", "classpath "))
            ]
            blocks.append(f"{path}:\n" + "\n".join(picks[:60]))
        elif lower.endswith("pyproject.toml"):
            picks = [line.strip() for line in text.splitlines() if "=" in line and line.strip().startswith(("dependencies", "aiohttp", "fastapi", "flask", "django", "pydantic", "sqlalchemy", "uvicorn", "pandas", "numpy", "requests"))]
            blocks.append(f"{path}:\n" + "\n".join(picks[:60]))
        elif lower.endswith(("go.mod", "Cargo.toml")):
            blocks.append(f"{path}:\n" + "\n".join(text.splitlines()[:60]))
    return "\n\n".join(blocks)


def _parse_pom_dependencies(text: str) -> list[str]:
    """Extract ``groupId:artifactId`` pairs from every `<dependency>` block."""
    artifacts: list[str] = []
    seen: set[str] = set()
    for block in re.findall(r"<dependency>(.*?)</dependency>", text or "", re.DOTALL):
        group = re.search(r"<groupId>([^<]+)</groupId>", block)
        artifact = re.search(r"<artifactId>([^<]+)</artifactId>", block)
        if not artifact:
            continue
        value = f"{group.group(1)}:{artifact.group(1)}" if group else artifact.group(1)
        if value not in seen:
            seen.add(value)
            artifacts.append(value)
    return artifacts[:80]


def _pom_summary(text: str) -> str:
    lines: list[str] = []
    for tag in ("name", "description", "java.version", "spring-cloud.version"):
        match = re.search(rf"<{tag}>([^<]+)</{tag}>", text or "")
        if match:
            lines.append(f"{tag}: {match.group(1)}")
    deps = _parse_pom_dependencies(text)
    if deps:
        lines.append("dependencies:")
        lines.extend(f"- {dep}" for dep in deps[:40])
    return "\n".join(lines) or "Maven project"


def _build_configuration(repo: str, files: dict[str, str]) -> str:
    lines: list[str] = []
    for path in sorted(files):
        lower = path.lower()
        text = files[path]
        if lower.endswith("pom.xml"):
            lines.append(f"{path}:\n{_pom_summary(text)}")
        elif lower.endswith(("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "Jenkinsfile", "ci.yml")):
            head = "\n".join(text.splitlines()[:30])
            lines.append(f"{path}:\n{head}")
        elif lower.endswith(("pyproject.toml", "package.json", "build.gradle", "build.gradle.kts", "settings.gradle")):
            head = "\n".join(text.splitlines()[:40])
            lines.append(f"{path}:\n{head}")
    return "\n\n".join(lines)


def _extension_of(filename: str | None) -> str | None:
    if not filename or "." not in filename:
        return None
    return filename.rsplit(".", 1)[-1].lower().strip(".") or None


def _mime_of(filename: str | None) -> str | None:
    import mimetypes

    if not filename:
        return None
    return mimetypes.guess_type(filename)[0]
