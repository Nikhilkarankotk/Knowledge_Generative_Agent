"""Tests for source resolvers + GitHub analysis (`app.export.sources`)."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from app.export.registry import ExportPayload
from app.export.sources import (
    ExportLimits,
    ExportServices,
    _parse_pom_dependencies,
    build_github_analysis,
    detect_structure,
    resolve_sources,
)
from app.models import DocumentFile, ExportContext, ExportContextItem
from app.repositories import DocumentFileRepository


class FakeDocumentFileRepo:
    def __init__(self, session) -> None:
        self._repo = DocumentFileRepository(session)

    def find_by_session_and_filename(self, session_id: str, filename: str) -> DocumentFile | None:
        return self._repo.find_by_session_and_filename(session_id, filename)


class FakeConfluence:
    enabled = True
    pages: dict[str, str] = {}

    def get_page(self, page_id: str) -> str:
        return self.pages.get(page_id, "")


class FakeSharePoint:
    enabled = True

    def __init__(self, data: bytes | None = None, meta: dict | None = None) -> None:
        self._data = data or b"the native bytes"
        self._meta = meta or {"name": "policy.pdf", "mime_type": "application/pdf"}

    def download_document_bytes(self, *, document_id: str, drive_id: str) -> tuple[bytes, dict]:
        return self._data, self._meta


class FakeGitHub:
    enabled = True
    tree: list[dict] = []
    files: dict[str, str] = {}

    def get_repository(self, repo: str) -> str:
        return f"Name: {repo}\nDescription: a service"

    def get_readme(self, repo: str) -> str:
        return "[Source: GitHub: acme/api:README.md]\nREADME of acme/api\nPayment service"

    def walk_repository(
        self, repo: str, *, max_items: int, max_depth: int, skip_dirs: tuple[str, ...] = ()
    ) -> list[dict]:
        return self.tree

    def get_file_content_text(self, repo: str, path: str, *, char_limit: int | None = None) -> str:
        return self.files.get(path, "")


def _item(session, ctx: ExportContext, **kwargs) -> ExportContextItem:
    item = ExportContextItem(export_context_id=ctx.id, **kwargs)
    session.add(item)
    session.commit()
    return item


def test_detect_structure_kinds() -> None:
    assert detect_structure('{"a": 1}')["kind"] == "json"
    assert detect_structure("<h1>Title</h1><p>body</p>")["kind"] == "html"
    assert detect_structure("# Heading\n\nprose")["kind"] == "markdown"
    assert detect_structure("a,b\n1,2\n3,4")["kind"] == "tabular"
    assert detect_structure("just plain prose text.")["kind"] == "prose"
    assert detect_structure("")["kind"] == "prose"


def test_upload_resolves_native_bytes(db_session) -> None:
    session_id = "sess-native"
    ctx = ExportContext(chat_message_id=1, session_id=session_id)
    db_session.add(ctx)
    db_session.commit()
    db_session.add(
        DocumentFile(
            session_id=session_id,
            filename="policy.pdf",
            content=b"%PDF-xnative",
            content_type="application/pdf",
        )
    )
    db_session.commit()
    item = _item(
        db_session,
        ctx,
        source_type="UPLOADED_DOCUMENT",
        source_id="policy.pdf",
        source_name="policy.pdf",
        filename="policy.pdf",
        content_reference="policy content",
    )

    resolved = resolve_sources([item], session_id, services=ExportServices(document_file_repo=FakeDocumentFileRepo(db_session)))
    assert len(resolved) == 1
    source = resolved[0]
    assert source.has_native_bytes
    assert source.native_bytes == b"%PDF-xnative"
    assert source.native_format == "pdf"
    assert source.structure == {"kind": "prose", "rows": [], "headers": []}


def test_upload_without_stored_bytes_falls_back_to_captured(db_session) -> None:
    session_id = "sess-ghost"
    ctx = ExportContext(chat_message_id=1, session_id=session_id)
    db_session.add(ctx)
    db_session.commit()
    item = _item(
        db_session,
        ctx,
        source_type="UPLOADED_DOCUMENT",
        source_id="notes.pdf",
        source_name="notes.pdf",
        filename="notes.pdf",
        content_reference=__import__("json").dumps({"content": "captured pdf text"}),
    )

    resolved = resolve_sources([item], session_id, services=ExportServices(document_file_repo=FakeDocumentFileRepo(db_session)))
    assert resolved[0].has_native_bytes is False
    assert resolved[0].content == "captured pdf text"


def test_confluence_enriched_when_service_enabled(db_session) -> None:
    fake = FakeConfluence()
    fake.pages = {
        "1234": (
            "[Source: Confluence: Fresh (space: FIN)]\n"
            "https://wiki/fresh\n"
            "enriched content with a long paragraph to describe the policy"
        )
    }
    ctx = ExportContext(chat_message_id=1, session_id="s1")
    db_session.add(ctx)
    db_session.commit()
    item = _item(
        db_session,
        ctx,
        source_type="CONFLUENCE",
        source_id="1234",
        source_name="Stale",
        content_reference="stale excerpt",
    )

    (source,) = resolve_sources(
        [item], "s1", services=ExportServices(confluence_service=fake)  # type: ignore[arg-type]
    )
    assert "enriched content with a long paragraph to describe the policy" in source.content
    assert source.source_name == "Fresh"
    assert source.source_url == "https://wiki/fresh"


def test_confluence_uses_captured_when_service_off(db_session) -> None:
    off = SimpleNamespace(enabled=False)
    ctx = ExportContext(chat_message_id=1, session_id="s1")
    db_session.add(ctx)
    db_session.commit()
    item = _item(
        db_session,
        ctx,
        source_type="CONFLUENCE",
        source_id="1234",
        source_name="Page",
        content_reference="captured piece",
    )
    (source,) = resolve_sources([item], "s1", services=ExportServices(confluence_service=off))  # type: ignore[arg-type]
    assert source.content == "captured piece"


def test_sharepoint_native_download(db_session) -> None:
    ctx = ExportContext(chat_message_id=1, session_id="s1")
    db_session.add(ctx)
    db_session.commit()
    item = _item(
        db_session,
        ctx,
        source_type="SHAREPOINT",
        source_id="7",
        source_name="policy.pdf",
        content_reference="captured sharepoint text",
        mime_type="application/pdf",
        meta={"drive_id": "d1", "document_id": "7", "filename": "policy.pdf"},
    )
    fake = FakeSharePoint(data=b"%PDF-share", meta={"name": "policy.pdf", "mime_type": "application/pdf", "web_url": "https://share/policy"})
    (source,) = resolve_sources([item], "s1", services=ExportServices(sharepoint_service=fake))  # type: ignore[arg-type]
    assert source.has_native_bytes
    assert source.native_bytes == b"%PDF-share"
    assert source.native_format == "pdf"


def test_unknown_source_type_is_never_exported(db_session) -> None:
    ctx = ExportContext(chat_message_id=1, session_id="s1")
    db_session.add(ctx)
    db_session.commit()
    item = _item(db_session, ctx, source_type="MYSTERY", source_id="x", source_name="x")
    assert resolve_sources([item], "s1") == []


def _github_setup(db_session) -> ExportContextItem:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "dir", "path": "src"},
        {"type": "file", "path": "src/main.py"},
        {"type": "file", "path": "src/payments.py"},
        {"type": "file", "path": "package.json"},
        {"type": "file", "path": ".env.production"},
        {"type": "file", "path": "README.md"},
    ]
    fake.files = {
        "src/main.py": "@app.get('/health')\ndef health(): return 'ok'",
        "src/payments.py": '@router.post("/payments")',
        "package.json": '{"dependencies": {"fastapi": "^0.1"}}',
        ".env.production": "SECRET_KEY=supersecret avx",
    }
    ctx = ExportContext(chat_message_id=1, session_id="s1")
    db_session.add(ctx)
    db_session.commit()
    item = _item(
        db_session,
        ctx,
        source_type="GITHUB",
        source_id="acme/api",
        source_name="acme/api",
        content_reference="retrieved github context",
    )
    return item, fake  # type: ignore[return-value]


def test_build_github_analysis_is_evidence_driven(db_session) -> None:
    item, fake = _github_setup(db_session)  # type: ignore[misc]
    (source,) = resolve_sources([item], "s1", services=ExportServices(github_service=fake), limits=ExportLimits())  # type: ignore[arg-type]
    assert source.analysis is not None
    assert isinstance(source.analysis, ExportPayload)
    section_text = "\n".join(s.body for s in source.analysis.sections)
    assert "/health" in section_text
    assert "fastapi" in section_text
    assert "SECRET_KEY" not in section_text
    assert ".env.production" not in "\n".join(f"{path}: {text}" for path, text in source.sampled_sources)
    assert {path for path, _ in source.sampled_sources} == {
        "src/main.py",
        "src/payments.py",
        "package.json",
    }


def test_github_disabled_produces_retrieved_context_analysis(db_session) -> None:
    ctx = ExportContext(chat_message_id=1, session_id="s1")
    db_session.add(ctx)
    db_session.commit()
    item = _item(
        db_session,
        ctx,
        source_type="GITHUB",
        source_id="acme/api",
        source_name="acme/api",
        content_reference="only retrieved text",
    )
    (source,) = resolve_sources([item], "s1", services=ExportServices(github_service=SimpleNamespace(enabled=False)))  # type: ignore[arg-type]
    assert source.analysis is not None
    assert source.analysis.sections[0].body == "only retrieved text"
    assert any(s.evidence == "Unavailable" for s in source.analysis.sections)


def test_build_github_analysis_direct() -> None:
    fake = FakeGitHub()
    fake.tree = [{"type": "file", "path": "src/app.py"}]
    fake.files = {"src/app.py": 'HTTP.get("/ping")\ncode'}
    payload, sampled = build_github_analysis("acme/api", fake, limits=ExportLimits())
    text = "\n".join(s.body for s in payload.sections)
    assert "acme/api" in payload.title
    assert "/ping" in text
    assert sampled == [("src/app.py", 'HTTP.get("/ping")\ncode')]


def test_github_analysis_traces_code_flow_and_functions() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "file", "path": "src/main.py"},
        {"type": "file", "path": "src/payments.py"},
        {"type": "file", "path": "src/models.py"},
    ]
    fake.files = {
        "src/main.py": (
            "from payments import create_payment\n"
            "def main():\n"
            "    app = create_app()\n"
            "    app.run(host='0.0.0.0', port=8080)\n"
        ),
        "src/payments.py": (
            "from models import Payment\n"
            "def create_payment(user_id):\n"
            "    return Payment()\n"
        ),
        "src/models.py": "class Payment:\n    def __init__(self):\n        pass\n",
    }
    payload, sampled = build_github_analysis("acme/api", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    flow = by_heading["Entry Points & Code Flow"].body
    assert "src/main.py" in flow
    assert "create_app" in flow
    assert "main.py -> payments" in flow
    assert "payments.py -> models" in flow

    functions = by_heading["Functions & Classes (sampled)"].body
    assert "src/main.py:1: from payments" not in functions  # import line is not a def
    assert "src/main.py:2: def main(" in functions
    assert "src/payments.py:2: def create_payment(" in functions
    assert "src/models.py:1: class Payment" in functions

    functions_section = by_heading["Functions & Classes (sampled)"]
    assert functions_section.rows
    assert functions_section.headers == ["File:Line", "Signature"]
    assert functions_section.rows[0][1].startswith(("def ", "class ", "async "))

    endpoints_section = by_heading["API Endpoints"]
    assert endpoints_section.headers == ["File", "Definition"]
    assert endpoints_section.rows == []  # no route patterns in these sampled files

    structure_section = by_heading["Repository Structure"]
    assert structure_section.rows
    assert structure_section.headers == ["Type", "Path"]
    assert any(row[0] == "file" for row in structure_section.rows)


def test_github_analysis_embeds_sampled_source_in_report() -> None:
    fake = FakeGitHub()
    fake.tree = [{"type": "file", "path": "src/main.py"}]
    fake.files = {"src/main.py": "def main():\n    print('hello')\n    print('world')\n"}
    payload, _ = build_github_analysis("acme/api", fake, limits=ExportLimits())
    file_sections = [s for s in payload.sections if s.heading.startswith("File: src/main.py")]
    assert file_sections
    assert "print('hello')" in file_sections[0].body
    assert file_sections[0].evidence == "Observed from source"


def test_parse_maven_dependencies_from_pom() -> None:
    pom = (
        "<project><dependencies>"
        "<dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId></dependency>"
        "<dependency><artifactId>lombok</artifactId></dependency>"
        "<dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId></dependency>"
        "</dependencies></project>"
    )
    assert _parse_pom_dependencies(pom) == [
        "org.springframework.boot:spring-boot-starter-web",
        "lombok",
    ]


def test_github_analysis_samples_deep_java_sources() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "dir", "path": "Products/src/main/java"},
        {"type": "file", "path": "Products/src/main/java/com/nkk/ProductsApplication.java"},
        {"type": "file", "path": "Products/pom.xml"},
    ]
    pom = (
        "<project><name>Products</name><description>Products service</description>"
        "<properties><java.version>21</java.version>"
        "<spring-cloud.version>2024.0.0</spring-cloud.version></properties>"
        "<dependencies><dependency><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-starter-web</artifactId></dependency></dependencies></project>"
    )
    fake.files = {
        "Products/pom.xml": pom,
        "Products/src/main/java/com/nkk/ProductsApplication.java": (
            "@SpringBootApplication\n"
            "public class ProductsApplication {\n"
            "  public static void main(String[] args) {}\n"
            "}"
        ),
    }
    payload, sampled = build_github_analysis("acme/platform", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    assert any(path.endswith(".java") for path, _ in sampled)

    flow = by_heading["Entry Points & Code Flow"].body
    assert "ProductsApplication.java" in flow
    assert "@SpringBootApplication" in flow

    dep_text = by_heading["Dependencies & Technology Stack"].body
    assert "spring-boot-starter-web" in dep_text
    assert "<dependency>" not in dep_text

    build_text = by_heading["Build & Configuration"].body
    assert "java.version: 21" in build_text
    assert "spring-boot-starter-web" in build_text
    assert "<dependency>" not in build_text

    modules = by_heading["Modules / Services"].body
    assert "Products" in modules
    assert ".gitignore" not in modules


def test_github_analysis_emits_architecture_diagram() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "file", "path": "src/main/java/com/acme/MainApplication.java"},
        {"type": "file", "path": "src/main/java/com/acme/ProductController.java"},
        {"type": "file", "path": "src/main/java/com/acme/ProductService.java"},
        {"type": "file", "path": "src/main/java/com/acme/ProductRepository.java"},
        {"type": "file", "path": "src/test/java/com/acme/ProductControllerTest.java"},
    ]
    fake.files = {
        "src/main/java/com/acme/MainApplication.java": "@SpringBootApplication\npublic class MainApplication {}",
        "src/main/java/com/acme/ProductController.java": "@RestController\nclass ProductController {}",
        "src/main/java/com/acme/ProductService.java": "class ProductService {}",
        "src/main/java/com/acme/ProductRepository.java": "@Repository\nclass ProductRepository {}",
        "src/test/java/com/acme/ProductControllerTest.java": "class ProductControllerTest {}",
    }
    payload, _ = build_github_analysis("acme/api", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    diagram = by_heading["System Architecture"].body
    assert "ENTRY POINT" in diagram
    assert "API / REST LAYER" in diagram
    assert "ProductController" in diagram
    assert "ProductService" in diagram
    assert "ProductRepository" in diagram
    assert "External clients" in diagram
    assert "Data / persistence" in diagram
    # test sources are noise and never classified into layers
    assert "ControllerTest" not in diagram

    if importlib.util.find_spec("PIL"):
        assert payload.diagram_png
        assert payload.diagram_png.startswith(b"\x89PNG")


def test_github_analysis_emits_data_model_section() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "file", "path": "src/main/java/com/acme/Customer.java"},
        {"type": "file", "path": "src/main/java/com/acme/CustomerController.java"},
        {"type": "file", "path": "src/main/java/com/acme/CustomerRepository.java"},
    ]
    fake.files = {
        "src/main/java/com/acme/Customer.java": (
            "@Entity\n"
            "public class Customer {\n"
            "  @Id private Long id;\n"
            "  @Column(name=\"customer_name\") private String name;\n"
            "  private String email;\n"
            "}"
        ),
        "src/main/java/com/acme/CustomerController.java": (
            "@RestController\n"
            "@RequestMapping(value=\"/customers\")\n"
            "class CustomerController {\n"
            "  @GetMapping(value=\"/{id}\") public Customer get(@PathVariable Long id) { return null; }\n"
            "}"
        ),
        "src/main/java/com/acme/CustomerRepository.java": (
            "public interface CustomerRepository extends JpaRepository<Customer, Long> {\n"
            "  List<Customer> findByName(String name);\n"
            "}"
        ),
    }
    payload, _ = build_github_analysis("acme/api", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    data_models = by_heading["Data Models & Schema"].body
    assert "Customer" in data_models
    assert "customer_name" in data_models
    assert "Long" in data_models


def test_github_analysis_emits_service_layer_section() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "file", "path": "src/main/java/com/acme/OrderService.java"},
    ]
    fake.files = {
        "src/main/java/com/acme/OrderService.java": (
            "@Service\n"
            "public class OrderService {\n"
            "  public Order place(OrderRequest request) { return null; }\n"
            "  public void cancel(Long id) {}\n"
            "}"
        ),
    }
    payload, _ = build_github_analysis("acme/api", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    services = by_heading["Service Layer & Business Logic Resources"].body
    assert "OrderService" in services
    assert "place" in services
    assert "cancel" in services


def test_github_analysis_emits_api_contract_rows() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "file", "path": "src/main/java/com/acme/ProductController.java"},
        {"type": "file", "path": "src/router.py"},
    ]
    fake.files = {
        "src/main/java/com/acme/ProductController.java": (
            "@RestController\n"
            "@RequestMapping(value=\"/products\")\n"
            "class ProductController {\n"
            "  @PostMapping(value=\"/create\") public Product create(@RequestBody Product body) { return null; }\n"
            "}"
        ),
        "src/router.py": (
            "router = APIRouter()\n"
            "@router.post(\"/items\")\n"
            "def create_item(item_id: int):\n"
            "    return {'ok': True, 'id': item_id}\n"
        ),
    }
    payload, _ = build_github_analysis("acme/api", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    contract = by_heading["HTTP API Contract Details"]
    assert contract.headers == ["Method", "Path", "Source", "Parameters"]
    assert contract.rows
    methods = {row[0] for row in contract.rows}
    assert "POST" in methods
    paths = {row[1] for row in contract.rows}
    assert "/items" in paths
    assert "/create" in paths
    assert "/products" in paths
    assert {row[2] for row in contract.rows} == {
        "src/router.py",
        "src/main/java/com/acme/ProductController.java",
    }


def test_github_analysis_emits_external_integrations_section() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "file", "path": "src/main/java/com/acme/MistralClient.java"},
    ]
    fake.files = {
        "src/main/java/com/acme/MistralClient.java": (
            "public class MistralClient {\n"
            "  private WebClient client = WebClient.builder()"
            ".baseUrl(\"https://api.mistral.ai/v1\").build();\n"
            "  public void call() {}\n"
            "}"
        ),
    }
    payload, _ = build_github_analysis("acme/api", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    integration = by_heading["External Integrations"].body
    assert "api.mistral.ai" in integration
    assert "HTTP client" in integration


def test_github_analysis_emits_configuration_summary() -> None:
    fake = FakeGitHub()
    fake.tree = [
        {"type": "file", "path": "src/main/resources/application.yml"},
        {"type": "file", "path": "src/config.py"},
    ]
    fake.files = {
        "src/main/resources/application.yml": (
            "server:\n"
            "  port: 8080\n"
            "spring:\n"
            "  datasource:\n"
            "    url: jdbc:postgresql://localhost/db\n"
        ),
        "src/config.py": (
            "class Settings:\n"
            "    api_key: str\n"
            "    model_name: str = 'mistral-small-latest'\n"
        ),
    }
    payload, _ = build_github_analysis("acme/api", fake, limits=ExportLimits())
    by_heading = {section.heading: section for section in payload.sections}

    config = by_heading["Configuration Summary"].body
    assert "application.yml" in config
    assert "8080" in config
    assert "local" in config
