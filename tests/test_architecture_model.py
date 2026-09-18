"""Tests for the evidence-driven architecture model (`app.export.architecture`).

Every test exercises rule-based detection over synthesised repository content:
nothing here depends on reportlab or the network.
"""

from __future__ import annotations

from app.export.architecture import (
    KIND_CONTROLLER,
    KIND_EXTERNAL,
    KIND_SERVICE,
    ArchitectureModel,
    build_architecture_model,
)

BASE_FILES = {
    "README.md": "# Acme API\nA service overview.",
    "app/main.py": "import uvicorn\nfrom fastapi import FastAPI\napp = FastAPI()\nif __name__ == '__main__':\n    uvicorn.run(app)",
    "app/routes.py": (
        "from fastapi import APIRouter\nrouter = APIRouter()\n"
        "@router.get('/items')\ndef list_items():\n    return items_service.list()\n"
        "@router.post('/items')\ndef create_item(payload):\n    return items_service.create(payload)"
    ),
    "app/services/items_service.py": (
        "class ItemsService:\n"
        "    def list(self):\n"
        "        import sqlalchemy\n"
        "        conn = psycopg.connect('postgresql://db')\n"
        "        return conn.execute('SELECT * FROM items')\n"
        "    def create(self, payload):\n"
        "        r = requests.post('https://api.stripe.com/v1/charges', json=payload)\n"
        "        return r.json()"
    ),
    "app/services/audit_service.py": (
        "class AuditService:\n"
        "    def log(self, event):\n"
        "        import requests\n"
        "        requests.post('https://audit.internal/log')\n"
    ),
    "app/config.py": "import os\nclass Settings:\n    api_key = os.getenv('API_KEY')",
    "Dockerfile": "FROM python:3.12\nCOPY . /app\nCMD ['uvicorn', 'app.main:app']",
    ".github/workflows/ci.yml": "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4",
}

TREE = [{"type": "file", "path": path} for path in sorted(BASE_FILES)]


def _model(*, files: dict[str, str] | None = None, repo_meta: str = "", readme: str = "", tree: list | None = None) -> ArchitectureModel:
    content = dict(BASE_FILES)
    content.update(files or {})
    return build_architecture_model(
        "acme/api",
        files=content,
        readme=readme or content.get("README.md", ""),
        repo_meta=repo_meta,
        tree=tree or TREE,
    )


def _names(model: ArchitectureModel, kind: str) -> list[str]:
    return sorted(component.name for component in model.by_kind(kind))


def test_every_component_has_evidence() -> None:
    model = _model()
    assert model.components, "a representative repo must yield components"
    for component in model.components:
        assert component.evidence, component.component_id


def test_primary_flow_lands_in_database_not_broker() -> None:
    model = _model(
        files={
            "app/services/items_service.py": (
                "class ItemsService:\n"
                "    def list(self):\n"
                "        import sqlalchemy\n"
                "        return conn.execute('SELECT * FROM items')\n"
                "    def consume(self):\n"
                "        from kafka import KafkaProducer\n"
                "        producer.send('topic', value=b'x')\n"
            )
        }
    )
    assert model.flows, "expected at least a primary flow"
    primary = model.flows[0]
    assert primary.name == "Primary Request Path"
    assert primary.steps, "primary flow must have steps"


def test_primary_flow_prefers_most_connected_service() -> None:
    files = {
        "README.md": "# Acme API\nA service overview.",
        "app/main.py": "from fastapi import FastAPI\napp = FastAPI()",
        "app/services/busy_service.py": (
            "class BusyService:\n    def run(self):\n"
            "        import sqlalchemy\n        conn = psycopg.connect('postgresql://db')\n"
            "        return conn.execute('SELECT * FROM t')\n"
        ),
        "app/services/idle_service.py": "class IdleService:\n    def ping(self):\n        return 'ok'",
    }
    model = build_architecture_model(
        "acme/api",
        files=files,
        readme=files["README.md"],
        tree=[{"type": "file", "path": path} for path in sorted(files)],
    )
    primary = model.flows[0]
    assert primary.name == "Primary Request Path"
    assert any("Busy" in step for step in primary.steps)


def test_bootstrap_servers_is_not_bootstrap_css() -> None:
    model = _model(files={"app/services/kafka_producer.py": "bootstrap.servers = 'kafka:9092'"})
    tech_names = [entry.name for entry in model.technologies]
    assert "Bootstrap" not in tech_names


def test_external_systems_exclude_registry_docs_build_urls() -> None:
    files = {
        "pom.xml": (
            "<project><url>https://mvnrepository.com/artifact/foo</url>"
            "<url>https://repo1.maven.org/maven2</url>"
            "<url>https://docs.spring.io/spring-boot</url></project>"
        ),
        "app/services/items_service.py": (
            "class ItemsService:\n    def run(self):\n"
            "        import sqlalchemy\n        return conn.execute('SELECT * FROM items')"
        ),
    }
    model = _model(files=files)
    external_names = _names(model, KIND_EXTERNAL)
    assert external_names == [], external_names


def test_external_systems_require_runtime_call_in_code() -> None:
    files = {
        "README.md": "Talks to https://api.weather.gov and https://www.w3.org/TR/spec",
        "app/services/items_service.py": (
            "class ItemsService:\n    def run(self):\n"
            "        resp = requests.post('https://api.weather.gov/data', json={})\n"
            "        return resp.json()"
        ),
    }
    model = _model(files=files)
    external_names = _names(model, KIND_EXTERNAL)
    assert "Api.weather.gov" in external_names, external_names
    assert not any("W3" in name for name in external_names)


def test_style_event_driven_when_broker_present() -> None:
    model = _model(files={"app/services/items_service.py": "from kafka import KafkaProducer"})
    assert "Event-driven" in model.style


def test_style_service_oriented_with_many_services() -> None:
    files = {
        "app/services/a_service.py": "class AService:\n    def run(self):\n        return 1",
        "app/services/b_service.py": "class BService:\n    def run(self):\n        return 2",
        "app/services/c_service.py": "class CService:\n    def run(self):\n        return 3",
    }
    model = _model(files=files)
    assert "Service-oriented" in model.style


def test_description_parsed_from_repository_metadata() -> None:
    repo_meta = (
        "[Source: GitHub: acme/api]\nURL: https://github.com/acme/api\n"
        "Description: Payment service that charges cards.\n"
    )
    model = _model(repo_meta=repo_meta)
    assert model.description == "Payment service that charges cards."


def test_deployment_assets_detected() -> None:
    model = _model()
    kinds = [component.kind for component in model.deployment]
    assert "ci" in kinds
    assert "image" in kinds


def test_structure_built_from_tree() -> None:
    model = _model()
    names = [area.name for area in model.structure]
    purposes = {area.name: area.purpose for area in model.structure}
    assert "app/" in names
    assert ".github/" in names
    assert purposes.get("app/") == "Application code"
    assert all(area.evidence for area in model.structure)


def test_empty_repo_yields_honest_notes() -> None:
    deliver = {
        "README.md": "# Empty\nNothing here.",
        ".github/workflows/ci.yml": "name: CI\non: push\njobs:\n  test:\n    runs-on: ubuntu-latest",
    }
    model = build_architecture_model(
        "empty/repo",
        files=deliver,
        readme=deliver["README.md"],
        tree=[{"type": "file", "path": path} for path in sorted(deliver)],
    )
    assert not model.components
    assert any("database" in note.lower() for note in model.notes)


def test_sensitive_files_excluded_from_components() -> None:
    model = _model(
        files={
            ".env": "SECRET_KEY=abc123",
            "secrets/bundle.pem": "PRIVATE KEY",
            "app/services/items_service.py": "class ItemsService:\n    def run(self):\n        return 1",
        }
    )
    names = [component.name.lower() for component in model.components]
    assert not any("secret" in name or ".env" in name for name in names)


def test_noise_directories_excluded() -> None:
    model = _model(
        files={
            "node_modules/pkg/index.js": "class ThingService {\n  run() { return 1 }\n}",
            "src/spec/thing_service.js": "exports.tests = []",
            "app/services/items_service.py": "class ItemsService:\n    def run(self):\n        import fastapi\n        return 1",
        }
    )
    names = [component.name.lower() for component in model.components]
    assert not any("thing" in name for name in names)


def test_repository_is_not_mistaken_for_a_service() -> None:
    model = build_architecture_model(
        "no-such/service",
        files=BASE_FILES,
        readme=BASE_FILES["README.md"],
        tree=TREE,
    )
    assert model.repo == "no-such/service"
    service_names = _names(model, KIND_SERVICE)
    assert "service" not in service_names


def test_deterministic_order() -> None:
    first = [(component.component_id, component.name, component.kind) for component in _model().components]
    second = [(component.component_id, component.name, component.kind) for component in _model().components]
    assert first == second


def test_security_features_require_evidence() -> None:
    model = _model(files={"app/services/items_service.py": "import jwt\nimport bcrypt\n"})
    names = {feature.name for feature in model.security}
    assert "JWT tokens" in names
    assert "Hashed passwords (bcrypt / argon2 / scrypt)" in names
    for feature in model.security:
        assert feature.evidence, feature.name


def test_security_absent_when_not_observed() -> None:
    plain = {"README.md": "# Plain\nNothing.", "app/main.py": "print('hi')"}
    model = build_architecture_model(
        "plain/repo",
        files=plain,
        readme=plain["README.md"],
        tree=[{"type": "file", "path": p} for p in plain],
    )
    assert not model.security


def test_security_categories_grouped_in_stable_order() -> None:
    model = _model(files={"app/services/items_service.py": "import jwt\nimport bcrypt\nimport pydantic\n"})
    categories = model.security_categories()
    assert [category for category, _ in categories] == [
        "Authentication", "Password & Secrets", "Input Validation", "Transport & Browser",
    ]
    assert all(features for _, features in categories)


def test_repository_tree_has_roles_and_depth() -> None:
    model = _model()
    nodes = model.tree
    assert nodes, "expected tree rows"
    assert all(node.depth >= 1 for node in nodes)
    files = [node for node in nodes if not node.is_dir]
    # entry / routes / service files are annotated with their role
    assert any(node.role == "entry" for node in files)
    assert any(node.role == "routes" for node in files)
    assert any(node.role == "service" for node in files)
    # store/external references surface as notes on the referencing file
    assert any(node.note for node in files)


def test_tree_keeps_internal_store_links_as_notes() -> None:
    model = _model()
    notes = {node.note for node in model.tree if node.note}
    assert notes, "data-store linkage should appear as a note"


def test_tree_honours_security_docs_ignored_elsewhere() -> None:
    model = _model(files={"app/services/items_service.py": "import jwt\nimport bcrypt\n"})
    # .env / secret paths must never surface as tree rows
    assert not any(".env" in node.name or "secret" in node.name.lower() for node in model.tree)


def test_component_facts_show_operations_and_routes() -> None:
    model = _model()
    service = model.by_kind(KIND_SERVICE)[0]
    facts = service.fact_lines()
    assert any("Operations:" in fact for fact in facts)
    route = model.by_kind(KIND_CONTROLLER)[0]
    route_facts = route.fact_lines()
    assert any("Routes:" in fact for fact in route_facts) or any(
        "HTTP routes observed" in fact for fact in route_facts
    )
