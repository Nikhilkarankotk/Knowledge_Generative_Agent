"""Evidence-driven architecture model extracted from a GitHub repository.

The model is the single source of truth behind the graphical pages of the GitHub
PDF report. Every component, relationship, flow, technology and deployment
element is derived from files actually sampled from the repository — nothing is
invented. A component without at least one supporting file path is never added,
and stores / external systems / deployment assets that the repository does not
evidence are simply absent from the model.

Classification rules:
- Only *runtime* external systems are modelled. Package registries, build
  tooling URLs, dependency metadata and documentation URLs (mvnrepository, W3,
  pypi, npm, …) are never shown as external systems. Runtime externals require
  a code file that either imports the SDK or references the remote endpoint.
- Deployment assets are only listed when actually found in the repository.

The model is code-shaped: dataclasses only, no LLM, no network access.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Component kinds and layers
# ---------------------------------------------------------------------------

KIND_CLIENT = "client"
KIND_GATEWAY = "gateway"
KIND_CONTROLLER = "controller"
KIND_SERVICE = "service"
KIND_DATABASE = "database"
KIND_CACHE = "cache"
KIND_MESSAGE_BROKER = "message_broker"
KIND_EXTERNAL = "external"
KIND_CROSS_CUTTING = "cross_cutting"

LAYER_CLIENT = "Client / UI"
LAYER_GATEWAY = "API Gateway / Entry"
LAYER_SERVICES = "Services & Modules"
LAYER_DATA = "Data & Infrastructure"
LAYER_EXTERNAL = "External Systems"

_KIND_LAYER = {
    KIND_CLIENT: LAYER_CLIENT,
    KIND_GATEWAY: LAYER_GATEWAY,
    KIND_CONTROLLER: LAYER_GATEWAY,
    KIND_SERVICE: LAYER_SERVICES,
    KIND_DATABASE: LAYER_DATA,
    KIND_CACHE: LAYER_DATA,
    KIND_MESSAGE_BROKER: LAYER_DATA,
    KIND_EXTERNAL: LAYER_EXTERNAL,
}


def _layer_for(kind: str) -> str:
    return _KIND_LAYER.get(kind, LAYER_SERVICES)


# ---------------------------------------------------------------------------
# Filename hygiene (mirrors the export reading filters).
# ---------------------------------------------------------------------------

_NOISE_SEGMENTS = {
    ".git", ".idea", ".vscode", "node_modules", "venv", ".venv", "__pycache__",
    "coverage", ".pytest_cache", ".mypy_cache", "target", "build", "dist", "out",
    ".next", ".nuxt", "test", "tests", "spec", "e2e", "testdata", "__mocks__",
}

_SENSITIVE_MARKERS = (
    ".env", "secret", "credential", "token", ".pem", "id_rsa",
    ".p12", ".jks", "keystore", "apikey", "api_key",
)


def _is_noise(path: str) -> bool:
    segments = (path or "").lower().split("/")
    return any(segment in _NOISE_SEGMENTS for segment in segments)


def _is_sensitive(path: str) -> bool:
    lower = ("/" + (path or "").lstrip("/")).lower()
    return any(marker in lower for marker in _SENSITIVE_MARKERS)


def _clean_path(path: str) -> str:
    cleaned = (path or "").strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _dedupe(values: list[str], *, limit: int = 3) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return seen


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "component"


def _unique_ids(components: list[ArchitectureComponent]) -> list[ArchitectureComponent]:
    used: Counter[str] = Counter()
    for component in components:
        used[component.component_id] += 1
    if all(count == 1 for count in used.values()):
        return components
    seen: Counter[str] = Counter()
    for component in components:
        seen[component.component_id] += 1
        if seen[component.component_id] > 1:
            component.component_id = (
                f"{component.component_id}-{seen[component.component_id]}"
            )
    return components


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class ArchitectureComponent:
    """A single process, service, store or boundary drawn on the diagrams.

    ``evidence`` is mandatory in spirit: every component the builder appends has
    at least one repository path backing it.
    """

    component_id: str
    name: str
    kind: str
    layer: str
    responsibility: str = ""
    evidence: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def primary_evidence(self) -> str:
        return self.evidence[0] if self.evidence else ""

    def fact_lines(self, limit: int = 4) -> list[str]:
        """Short, evidence-backed lines of content for the component's box.

        These are what make the diagrams explain the code rather than merely
        label it: operations, routes, owning type and where the component is
        referenced.
        """
        facts: list[str] = []

        methods = [str(m) for m in self.detail.get("methods") or []]
        if methods:
            facts.append("Operations: " + ", ".join(methods[:5]))

        endpoints = [str(e) for e in self.detail.get("endpoints") or []]
        if endpoints:
            facts.append("Routes: " + ", ".join(endpoints[:4]))

        owner_type = self.detail.get("class")
        if owner_type:
            facts.append(f"Type: {owner_type}")

        if self.kind in (KIND_DATABASE, KIND_CACHE, KIND_MESSAGE_BROKER):
            if self.evidence:
                facts.append("Referenced in: " + ", ".join(self.evidence[:2]))
        elif self.kind == KIND_EXTERNAL:
            if self.evidence:
                facts.append("Invoked from: " + ", ".join(self.evidence[:2]))
        elif self.kind == KIND_GATEWAY and self.detail.get("entry"):
            facts.append(f"Entry: {self.detail['entry']}")

        if not facts and self.primary_evidence:
            facts.append(f"File: {self.primary_evidence}")

        return facts[:limit]


@dataclass
class ArchitectureRelationship:
    """Typed edge between two components (by ``component_id``)."""

    source: str
    target: str
    kind: str = "call"  # call | data | message | access
    label: str = ""
    evidence: str = ""


@dataclass
class ArchitectureFlow:
    """An ordered participant chain plus a one-sentence explanation."""

    name: str
    steps: list[str]  # component names, in sequence
    description: str = ""


@dataclass
class TechnologyEntry:
    """One detected technology, grouped under a category."""

    category: str
    name: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class DeploymentComponent:
    """One observed step of the deployment pipeline."""

    name: str
    kind: str  # repository | ci | image | orchestration | platform | iaas | cloud
    evidence: list[str] = field(default_factory=list)


@dataclass
class RepositoryArea:
    """One top-level repository area for the repository guide table."""

    name: str
    purpose: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class RepositoryNode:
    """One row of the repository tree / code-flow map."""

    name: str
    depth: int
    is_dir: bool
    role: str = ""  # entry | routes | service | data | cache | broker | external | config
    note: str = ""


@dataclass
class SecurityFeature:
    """One security control observed in the repository (rule-based, evidence-backed)."""

    category: str
    name: str
    description: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class ArchitectureModel:
    repo: str
    components: list[ArchitectureComponent] = field(default_factory=list)
    relationships: list[ArchitectureRelationship] = field(default_factory=list)
    flows: list[ArchitectureFlow] = field(default_factory=list)
    technologies: list[TechnologyEntry] = field(default_factory=list)
    deployment: list[DeploymentComponent] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    description: str = ""
    style: str = ""
    structure: list[RepositoryArea] = field(default_factory=list)
    security: list[SecurityFeature] = field(default_factory=list)
    tree: list[RepositoryNode] = field(default_factory=list)

    def component(self, component_id: str) -> ArchitectureComponent | None:
        for component in self.components:
            if component.component_id == component_id:
                return component
        return None

    def by_kind(self, kind: str) -> list[ArchitectureComponent]:
        return [component for component in self.components if component.kind == kind]

    def by_layer(self, layer: str) -> list[ArchitectureComponent]:
        return [component for component in self.components if component.layer == layer]

    def security_categories(self) -> list[tuple[str, list[SecurityFeature]]]:
        """Security features grouped by category, in a stable presentation order."""
        order: list[str] = []
        grouped: dict[str, list[SecurityFeature]] = {}
        for feature in self.security:
            if feature.category not in grouped:
                grouped[feature.category] = []
                order.append(feature.category)
            grouped[feature.category].append(feature)
        return [(category, grouped[category]) for category in order]


# ---------------------------------------------------------------------------
# Code-shape detectors (rules only; no guesses beyond observed text/paths).
# ---------------------------------------------------------------------------

_ENTRY_BASENAMES = {
    "main.py", "app.py", "server.js", "server.ts", "main.go", "main.java",
    "main.kt", "manage.py", "wsgi.py", "asgi.py", "index.js", "index.ts",
    "index.mjs", "__main__.py", "application.java", "program.cs",
}

_ENTRY_MARKERS = (
    "uvicorn.run", "if __name__ == ", "create_app(", "create_application(",
    "public static void main", "def app(", "const app = express(",
    "app.listen(", "app.run(", ".factory(", "@SpringBootApplication",
    "Application.run(", "from app import app", "FastAPI(",
)

_ROUTE_PATTERNS = (
    re.compile(r"@(?:app|router|bp|api)\.(?:get|post|put|delete|patch|route)\s*\(", re.IGNORECASE),
    re.compile(r"@(?:Get|Post|Put|Delete|Patch|Request)Mapping"),
    re.compile(r"router\.(?:get|post|put|delete|patch)\s*\(\s*[\"']/"),
    re.compile(r"(?:app\.get|app\.post|app\.put|app\.delete)\s*\(\s*[\"']/"),
    re.compile(r"\b(?:handleFunc|HandleFunc)\b"),
    re.compile(r"mapper\.(?:get|post|put|delete|patch)\b"),
)

_SERVICE_PATTERNS = (
    re.compile(r"@Service\b"),
    re.compile(r"class\s+\w*Service\b"),
    re.compile(r"class\s+\w*UseCase\b"),
    re.compile(r"class\s+\w*Manager\b"),
    re.compile(r"class\s+\w*Facade\b"),
    re.compile(r"\bdef\s+handle\b"),
)

_HTTP_CLIENT_TOKENS = (
    "httpx", "requests.", "aiohttp", "RestTemplate", "WebClient",
    "http.Client", "okhttp", "retrofit", "HttpURLConnection",
)

_PROTOCOL_TOKEN_SETS = (
    ("HTTP / REST", ("http.", "rest", "urlopen", "fetch(")),
    ("gRPC", ("grpc", "protobuf")),
    ("GraphQL", ("graphql",)),
    ("WebSocket", ("websocket", "ws://", "wss://")),
    ("AMQP", ("amqp", "rabbitmq")),
    ("SQL / JDBC", ("jdbc:", "sql")),
)

_CROSS_CUTTING = (
    ("Auth & Access Control", ("spring-security", "keycloak", "oauth", "jwt", "passport", "auth0", "cognito", "okta", "ldap", "saml")),
    ("Configuration", ("application.yml", "application.yaml", "application.properties", "bootstrap.yml", "settings.py", ".env.example", "Settings(")),
    ("Observability", ("prometheus", "grafana", "micrometer", "opentelemetry", "jaeger", "zipkin", "sentry", "structlog")),
    ("Service Discovery", ("eureka", "consul", "nacos", "istio", "envoy")),
    ("API Gateway", ("spring-cloud-gateway", "kong", "nginx", "traefik")),
)

_DATASTORE_RULES = (
    ("PostgreSQL", ("postgres", "postgresql", "jdbc:postgresql")),
    ("MySQL", ("mysql", "mariadb")),
    ("SQLite", ("sqlite",)),
    ("SQL Server", ("sql-server", "sqlserver", "mssql")),
    ("Oracle", ("oracle.jdbc", "ojdbc")),
    ("MongoDB", ("mongodb", "pymongo", "mongoose", "mongoengine")),
    ("Elasticsearch", ("elasticsearch", "opensearch")),
    ("DynamoDB", ("dynamodb",)),
    ("Cassandra", ("cassandra",)),
    ("DuckDB", ("duckdb",)),
    ("H2", ("h2database", "org.h2")),
)

_CACHE_RULES = (
    ("Redis", ("redis",)),
    ("Memcached", ("memcached",)),
    ("Ehcache", ("ehcache",)),
    ("Caffeine", ("caffeine",)),
    ("Spring Cache", ("cacheable",)),
)

_BROKER_RULES = (
    ("Kafka", ("kafka",)),
    ("RabbitMQ", ("rabbitmq", "amqp://")),
    ("ActiveMQ", ("activemq", "artemis")),
    ("Amazon SQS/SNS", ("sqs", "sns.")),
    ("Celery", ("celery",)),
    ("NATS", ("nats://", "nats.")),
    ("Pulsar", ("pulsar",)),
    ("WebSocket Hub", ("websocket", "socket.io")),
)

_TECH_CATALOG: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Frontend",
        (("React", "react"), ("Vue", "vue"), ("Angular", "angular"), ("Svelte", "svelte"),
         ("Next.js", "next"), ("Nuxt", "nuxt"), ("Tailwind CSS", "tailwind"), ("Bootstrap", "bootstrap"),
         ("Material UI", "@mui"), ("Ant Design", "antd"), ("jQuery", "jquery")),
    ),
    (
        "Backend Framework",
        (("FastAPI", "fastapi"), ("Flask", "flask"), ("Django", "django"), ("Express", "express"),
         ("Fastify", "fastify"), ("NestJS", "@nestjs"), ("Koa", "koa"), ("Spring Boot", "spring-boot"),
         ("Spring Cloud", "spring-cloud"), ("Spring MVC", "spring-webmvc"), ("Quarkus", "quarkus"),
         ("Micronaut", "micronaut"), ("Gin", "gin-gonic"), ("Echo", "echo"), ("ASP.NET Core", "aspnetcore"),
         ("Rails", "rails"), ("Laravel", "laravel")),
    ),
    (
        "Data Access / ORM",
        (("SQLAlchemy", "sqlalchemy"), ("Hibernate", "hibernate"), ("Spring Data JPA", "jpa"),
         ("MyBatis", "mybatis"), ("Sequelize", "sequelize"), ("Mongoose", "mongoose"),
         ("Prisma", "prisma"), ("GORM", "gorm"), ("SQLx", "sqlx"), ("Entity Framework", "entityframework"),
         ("Psycopg", "psycopg"), ("Django ORM", "django.db")),
    ),
    (
        "Database",
        (("PostgreSQL", "postgres"), ("MySQL", "mysql"), ("MongoDB", "mongodb"), ("SQLite", "sqlite"),
         ("Elasticsearch", "elasticsearch"), ("DynamoDB", "dynamodb"), ("DuckDB", "duckdb")),
    ),
    (
        "Cache",
        (("Redis", "redis"), ("Memcached", "memcached"), ("Ehcache", "ehcache"), ("Caffeine", "caffeine")),
    ),
    (
        "Messaging",
        (("Kafka", "kafka"), ("RabbitMQ", "rabbitmq"), ("Amazon SQS", "sqs"), ("Celery", "celery"),
         ("NATS", "nats"), ("Pulsar", "pulsar"), ("WebSocket", "websocket"), ("MQTT", "mqtt")),
    ),
    (
        "Security / Auth",
        (("Spring Security", "spring-security"), ("Keycloak", "keycloak"), ("OAuth / OIDC", "oauth"),
         ("JWT", "jwt"), ("Passport", "passport"), ("Auth0", "auth0"), ("AWS Cognito", "cognito"),
         ("Okta", "okta")),
    ),
    (
        "Observability",
        (("Prometheus", "prometheus"), ("Grafana", "grafana"), ("Micrometer", "micrometer"),
         ("OpenTelemetry", "opentelemetry"), ("Jaeger", "jaeger"), ("Zipkin", "zipkin"),
         ("Sentry", "sentry"), ("Datadog", "datadog"), ("New Relic", "newrelic"), ("ELK / Logstash", "logstash")),
    ),
    (
        "API / Data Protocol",
        (("REST / HTTP", "rest"), ("gRPC", "grpc"), ("GraphQL", "graphql"), ("OpenAPI / Swagger", "swagger"),
         ("Protobuf", "protobuf"), ("WebSocket", "websocket")),
    ),
    (
        "Container / Cloud",
        (("Docker", "docker"), ("Docker Compose", "compose"), ("Kubernetes", "kubernetes"),
         ("Helm", "helm"), ("Terraform", "terraform"), ("AWS", "aws"), ("Azure", "azure"),
         ("Google Cloud", "gcp"), ("Vercel", "vercel"), ("Heroku", "heroku"), ("Cloudflare", "cloudflare")),
    ),
    (
        "CI / CD",
        (("GitHub Actions", "github-actions"), ("GitHub Actions", ".github/workflows"), ("GitLab CI", "gitlab-ci"),
         ("Jenkins", "jenkins"), ("Travis CI", "travis"), ("CircleCI", "circleci"), ("Azure Pipelines", "azure-pipelines")),
    ),
    (
        "Service Mesh / Discovery",
        (("Eureka", "eureka"), ("Consul", "consul"), ("Nacos", "nacos"), ("Istio", "istio"), ("Envoy", "envoy")),
    ),
    (
        "Testing",
        (("pytest", "pytest"), ("JUnit", "junit"), ("Jest", "jest"), ("Cypress", "cypress"),
         ("Playwright", "playwright"), ("Selenium", "selenium"), ("Mockito", "mockito"),
         ("Mocha", "mocha"), ("Vitest", "vitest")),
    ),
)


def _token_re(tokens: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile("|".join(rf"\b{re.escape(tok)}\b" for tok in tokens), re.IGNORECASE)


def _first_identifier(text: str) -> str | None:
    patterns = (
        re.compile(r"^\s*(?:export\s+default\s+)?class\s+([A-Za-z_]\w*)"),
        re.compile(r"^\s*(?:class|def)\s+([A-Za-z_]\w*)"),
        re.compile(r"\bclass\s+([A-Za-z_]\w*)"),
        re.compile(r"def\s+([A-Za-z_]\w*)\s*\("),
        re.compile(r"func\s+([A-Za-z_]\w*)\s*\("),
    )
    for pattern in patterns:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return None


def _stem(path: str) -> str:
    base = (path or "").rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def _role_suffixes() -> tuple[str, ...]:
    return (
        "controller", "controllers", "service", "services", "router", "routes",
        "handler", "manager", "resource", "repository", "repositories",
    )


def _human_name(path: str, text: str) -> str:
    identifier = _first_identifier(text or "")
    if identifier:
        return identifier
    name = _stem(path)
    lowered = name.lower()
    for suffix in _role_suffixes():
        if lowered.endswith(suffix) and len(name) > len(suffix) + 1:
            return name[: -len(suffix)]
    return name or path


def _is_entry(path: str, text: str) -> bool:
    base = (path or "").rsplit("/", 1)[-1].lower()
    if base in _ENTRY_BASENAMES:
        return True
    head = (text or "")[:4000]
    return any(marker in head for marker in _ENTRY_MARKERS)


def _route_count(text: str) -> int:
    return sum(len(pattern.findall(text or "")) for pattern in _ROUTE_PATTERNS)


def _is_service_file(path: str, text: str) -> bool:
    if not text:
        return False
    lower_path = path.lower()
    if any(segment in lower_path.split("/") for segment in ("service", "services", "business", "usecase")):
        return True
    return any(pattern.search(text) for pattern in _SERVICE_PATTERNS)


def _first_docstring(text: str) -> str:
    doc = re.search(r"""((?:'|"){3})(.*?)\1""", text or "", re.DOTALL)
    if doc:
        summary = " ".join((doc.group(2) or "").split())
        return summary[:120]
    return ""


def _service_methods(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"def\s+(\w+)\s*\(|\bpublic\s+[\w<>\[\], ]+\s+(\w+)\s*\(", text or ""):
        name = match.group(1) or match.group(2)
        if name and not name.startswith("_"):
            found.append(name)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# Component discovery
# ---------------------------------------------------------------------------


def _entry_components(files: dict[str, str]) -> list[ArchitectureComponent]:
    components: list[ArchitectureComponent] = []
    seen: set[str] = set()
    for path in sorted(files):
        if _is_noise(path) or _is_sensitive(path):
            continue
        if path.lower().endswith((".html", ".css", ".csv", ".json", ".yml", ".yaml", ".md")):
            continue
        text = files[path]
        if text and not _is_entry(path, text):
            continue
        name = _human_name(path, text) or path
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        components.append(
            ArchitectureComponent(
                component_id=f"gateway:{_slug(name)}",
                name=name,
                kind=KIND_GATEWAY,
                layer=LAYER_GATEWAY,
                responsibility=(
                    _first_docstring(text)
                    or f"Application entry point; starts the HTTP server from {path}."
                ),
                evidence=[_clean_path(path)],
                detail={"entry": path},
            )
        )
    return components


def _controller_components(files: dict[str, str]) -> list[ArchitectureComponent]:
    components: list[ArchitectureComponent] = []
    seen: set[str] = set()
    for path in sorted(files):
        if _is_noise(path) or _is_sensitive(path):
            continue
        if not path.lower().endswith((".py", ".java", ".js", ".ts", ".go", ".kt", ".cs")):
            continue
        text = files[path]
        routes = _route_count(text)
        api_ish = routes > 0 or "@RestController" in text or "@Controller" in text
        if not api_ish:
            continue
        name = _human_name(path, text)
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        wording = f"Exposes {routes} HTTP route{'s' if routes != 1 else ''}"
        first_route = _first_route(text)
        if first_route:
            wording += f" (e.g. {first_route})"
        components.append(
            ArchitectureComponent(
                component_id=f"controller:{_slug(name)}",
                name=name,
                kind=KIND_CONTROLLER,
                layer=LAYER_GATEWAY,
                responsibility=wording,
                evidence=[_clean_path(path)],
                detail={"routes": routes, "endpoints": _route_paths(text), "verb_paths": _verbs_for_routes(text)},
            )
        )
    return components


_FIRST_ROUTE_RE = re.compile(r"""(?:get|post|put|delete|patch)\s*\(\s*["'](/[^"']*)["']""", re.IGNORECASE)
_ROUTE_PATH_RE = re.compile(
    r"""(?:get|post|put|delete|patch|route|handlefunc)\s*\(\s*["'](/[^"']*)["']""",
    re.IGNORECASE,
)
_SPRING_ROUTE_RE = re.compile(
    r"""(?:Mapping|value)\s*\(\s*(?:value\s*=\s*)?["'](/[^"']+)["']""",
    re.IGNORECASE,
)


def _first_route(text: str) -> str | None:
    match = _FIRST_ROUTE_RE.search(text or "")
    if match:
        return match.group(1)
    match = re.search(r'value\s*=\s*"([^"]+)"', text or "")
    if match:
        return match.group(1)
    return None


def _route_paths(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (_ROUTE_PATH_RE, _SPRING_ROUTE_RE):
        for match in pattern.finditer(text or ""):
            path = match.group(1)
            if path and path not in found:
                found.append(path)
    return found[:6]


def _verbs_for_routes(text: str) -> list[str]:
    verbs: list[str] = []
    for match in re.finditer(
        r"""@?\b(get|post|put|delete|patch)\s*(?:mapping)?\s*\(\s*["'](/[^"']*)["']""",
        text or "",
        re.IGNORECASE,
    ):
        entry = f"{match.group(1).upper()} {match.group(2)}"
        if entry not in verbs:
            verbs.append(entry)
    return verbs[:5]


def _service_components(files: dict[str, str], controllers: list[ArchitectureComponent]) -> list[ArchitectureComponent]:
    components: list[ArchitectureComponent] = []
    seen: set[str] = set()
    controller_paths = {component.primary_evidence for component in controllers}
    for path in sorted(files, key=lambda p: (p.count("/"), p)):
        if _is_noise(path) or _is_sensitive(path):
            continue
        if not path.lower().endswith((".py", ".java", ".js", ".ts", ".go", ".kt")):
            continue
        clean = _clean_path(path)
        if clean in controller_paths:
            continue
        lower_path = path.lower()
        if any(
            segment in lower_path.split("/")
            for segment in ("repository", "repositories", "dao", "mapper", "persistence")
        ) or any(
            lower_path.endswith(suffix)
            for suffix in ("repository.java", "repository.py", "repositories.java", "dao.java", "mapper.java")
        ):
            continue
        text = files[path]
        if not _is_service_file(path, text):
            continue
        name = _human_name(path, text)
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        methods = _service_methods(text)
        if methods:
            responsibility = f"Implements {len(methods)} operation(s): {', '.join(methods[:3])}."
        else:
            responsibility = _first_docstring(text) or f"Business logic owned by {name}."
        owner_class = _first_identifier(text)
        components.append(
            ArchitectureComponent(
                component_id=f"service:{_slug(name)}",
                name=name,
                kind=KIND_SERVICE,
                layer=LAYER_SERVICES,
                responsibility=responsibility,
                evidence=[clean],
                detail={"methods": methods, "class": owner_class or ""},
            )
        )
    return components


def _datastore_components(files: dict[str, str]) -> list[ArchitectureComponent]:
    found: dict[str, list[str]] = {}
    for path, text in files.items():
        if _is_noise(path) or _is_sensitive(path):
            continue
        for name, tokens in _DATASTORE_RULES:
            if _token_re(tokens).search(text or ""):
                found.setdefault(name, []).append(_clean_path(path))
    return [
        ArchitectureComponent(
            component_id=f"database:{_slug(name)}",
            name=name,
            kind=KIND_DATABASE,
            layer=LAYER_DATA,
            responsibility=f"Primary {name} store referenced by the application.",
            evidence=_dedupe(paths, limit=3),
        )
        for name, paths in sorted(found.items())
    ]


def _cache_components(files: dict[str, str]) -> list[ArchitectureComponent]:
    found: dict[str, list[str]] = {}
    for path, text in files.items():
        if _is_noise(path) or _is_sensitive(path):
            continue
        for name, tokens in _CACHE_RULES:
            if _token_re(tokens).search(text or ""):
                found.setdefault(name, []).append(_clean_path(path))
    return [
        ArchitectureComponent(
            component_id=f"cache:{_slug(name)}",
            name=name,
            kind=KIND_CACHE,
            layer=LAYER_DATA,
            responsibility="In-memory / distributed cache tier.",
            evidence=_dedupe(paths, limit=3),
        )
        for name, paths in sorted(found.items())
    ]


def _broker_components(files: dict[str, str]) -> list[ArchitectureComponent]:
    found: dict[str, list[str]] = {}
    for path, text in files.items():
        if _is_noise(path) or _is_sensitive(path):
            continue
        for name, tokens in _BROKER_RULES:
            if _token_re(tokens).search(text or ""):
                found.setdefault(name, []).append(_clean_path(path))
    return [
        ArchitectureComponent(
            component_id=f"broker:{_slug(name)}",
            name=name,
            kind=KIND_MESSAGE_BROKER,
            layer=LAYER_DATA,
            responsibility="Asynchronous messaging / event bus.",
            evidence=_dedupe(paths, limit=3),
        )
        for name, paths in sorted(found.items())
    ]


# External systems: only *runtime* integration points, only from code files.
# Package registries, build tooling, dependency metadata and documentation URLs
# are never modelled as external systems.
_EXTERNAL_IGNORED_SUBSTRINGS = (
    # package registries / dependency metadata
    "mvnrepository", "maven.apache", "repo1.maven", "central.sonatype", "maven-central",
    "plugins.gradle", "gradle.org", "repo.gradle", "npmjs", "yarnpkg", "yarn.pm",
    "pypi.org", "pythonhosted", "pypi.python", "nuget.org", "rubygems", "crates.io",
    "hub.docker", "dockerhub", "registry.",
    # documentation / spec / learning sites
    "w3.org", "rfc-editor", "ietf.org", "developer.mozilla", "readthedocs", "javadoc.io",
    "docs.spring", "spring.io", "react.dev", "learn.microsoft", "docs.microsoft",
    "kubernetes.io", "kafka.apache", "apache.org", "swagger.io", "openapis.org",
    "json-schema.org", "graphql.org", "grpc.io", "protobuf.dev", "owasp.org",
    "semver.org", "12factor", "microservices.io", "martinfowler", "wikipedia",
    "stackoverflow", "stackexchange", "medium.com", "blog.", "github.io",
    # build tooling
    "gradle", "maven", "sbt", "jenkins.io", "travis-ci",
    "example.com", "placeholder",
)

_EXTERNAL_CODE_EXTS = (".py", ".java", ".js", ".ts", ".jsx", ".tsx", ".go", ".kt", ".cs", ".rb", ".php", ".rs", ".scala")


def _is_external_ignored(host: str) -> bool:
    lowered = (host or "").lower()
    return any(marker in lowered for marker in _EXTERNAL_IGNORED_SUBSTRINGS)


def _external_components(files: dict[str, str]) -> list[ArchitectureComponent]:
    url_re = re.compile(r"https?://([a-zA-Z0-9.-]+\.[a-z]{2,})(?:[/:]|$)", re.IGNORECASE)
    skip_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "example.com", "github.com", "placeholder.com"}
    found: dict[str, list[str]] = {}
    sdk_rules = (
        ("stripe", "Stripe"), ("twilio", "Twilio"), ("sendgrid", "SendGrid"),
        ("aws-sdk", "AWS SDK"), ("google-cloud", "Google Cloud"), ("openai", "OpenAI"),
        ("anthropic", "Anthropic"), ("slack", "Slack"), ("discord", "Discord"),
        ("hubspot", "HubSpot"), ("salesforce", "Salesforce"), ("twitch", "Twitch"),
        ("spotify", "Spotify"), ("github api", "GitHub API"),
    )
    for path, text in files.items():
        if _is_noise(path) or _is_sensitive(path):
            continue
        clean = _clean_path(path)
        lowered = (text or "").lower()
        is_code = path.lower().endswith(_EXTERNAL_CODE_EXTS)
        for token, label in sdk_rules:
            if is_code and re.search(rf"\b{re.escape(token)}\b", lowered):
                found.setdefault(label, []).append(clean)
        if not is_code:
            continue
        for match in url_re.finditer(text or ""):
            host = match.group(1).lower()
            if host in skip_hosts or host.endswith((".local", ".internal", ".svc")):
                continue
            if _is_external_ignored(host):
                continue
            found.setdefault(host.capitalize(), []).append(clean)
    components: list[ArchitectureComponent] = []
    for name, paths in sorted(found.items()):
        components.append(
            ArchitectureComponent(
                component_id=f"external:{_slug(name)}",
                name=name,
                kind=KIND_EXTERNAL,
                layer=LAYER_EXTERNAL,
                responsibility="External system the application calls at runtime.",
                evidence=_dedupe(paths, limit=2),
            )
        )
    return components[:6]


def _cross_cutting_components(files: dict[str, str]) -> list[ArchitectureComponent]:
    found: dict[str, list[str]] = {}
    for path, text in files.items():
        if _is_noise(path) or _is_sensitive(path):
            continue
        clean = _clean_path(path)
        lowered = (text or "").lower()
        for name, tokens in _CROSS_CUTTING:
            if _token_re(tokens).search(lowered):
                found.setdefault(name, []).append(clean)
    return [
        ArchitectureComponent(
            component_id=f"cross:{_slug(name)}",
            name=name,
            kind=KIND_CROSS_CUTTING,
            layer=LAYER_SERVICES,
            responsibility="Cross-cutting capability applied across the services.",
            evidence=_dedupe(paths, limit=2),
        )
        for name, paths in sorted(found.items())
    ]


_CLIENT_DIRS = ("frontend", "web", "client", "ui", "public")


def _client_components(files: dict[str, str], tree: list[dict[str, Any]] | None) -> list[ArchitectureComponent]:
    evidence: list[str] = []
    for path in files:
        if _is_noise(path) or _is_sensitive(path):
            continue
        parts = path.lower().split("/")
        if any(dirname in parts for dirname in _CLIENT_DIRS) and path.lower().endswith(
            (".html", ".js", ".jsx", ".ts", ".tsx", ".vue", ".css")
        ):
            evidence.append(_clean_path(path))
    if tree and not evidence:
        for entry in tree or []:
            path = str(entry.get("path") or "").lower()
            if _is_noise(path) or _is_sensitive(path):
                continue
            parts = path.split("/")
            if any(dirname in parts for dirname in _CLIENT_DIRS) and path.endswith(".html"):
                evidence.append(_clean_path(path))
                break
    evidence = _dedupe(evidence, limit=3)
    if not evidence:
        return []
    return [
        ArchitectureComponent(
            component_id="client:web-ui",
            name="Web Client / UI",
            kind=KIND_CLIENT,
            layer=LAYER_CLIENT,
            responsibility="Browser-based user interface consuming the API.",
            evidence=evidence,
        )
    ]


# ---------------------------------------------------------------------------
# Relationships and flows
# ---------------------------------------------------------------------------


def _build_relationships(
    files: dict[str, str], components: list[ArchitectureComponent]
) -> list[ArchitectureRelationship]:
    relationships: list[ArchitectureRelationship] = []
    by_id = {component.component_id: component for component in components}
    added: set[tuple[str, str]] = set()

    def add(source: str, target: str, kind: str, label: str, evidence: str) -> None:
        if source == target or source not in by_id or target not in by_id:
            return
        key = (source, target)
        if key in added:
            return
        added.add(key)
        relationships.append(
            ArchitectureRelationship(source=source, target=target, kind=kind, label=label, evidence=evidence)
        )

    for owner in components:
        if owner.kind not in {KIND_GATEWAY, KIND_CONTROLLER, KIND_SERVICE, KIND_CLIENT}:
            continue
        path = owner.primary_evidence
        text = files.get(path, "")
        lowered = (text or "").lower()
        if not lowered:
            continue
        for other in components:
            if other is owner or other.kind == KIND_CLIENT:
                continue
            token = other.name.lower()
            if len(token) < 3 or other.kind == KIND_CROSS_CUTTING:
                continue
            if not re.search(rf"\b{re.escape(token)}\b", lowered):
                continue
            if other.kind == KIND_DATABASE:
                add(owner.component_id, other.component_id, "data", "reads / writes", path)
            elif other.kind == KIND_MESSAGE_BROKER:
                add(owner.component_id, other.component_id, "message", "publish / subscribe", path)
            elif other.kind == KIND_CACHE:
                add(owner.component_id, other.component_id, "data", "cache access", path)
            elif other.kind == KIND_EXTERNAL:
                add(owner.component_id, other.component_id, "access", "API call", path)
            elif other.kind in {KIND_SERVICE, KIND_CONTROLLER}:
                add(owner.component_id, other.component_id, "call", "delegates", path)

    clients = [component for component in components if component.kind == KIND_CLIENT]
    api_layer = [component for component in components if component.kind in {KIND_GATEWAY, KIND_CONTROLLER}]
    if clients and api_layer:
        client = clients[0]
        for extra in api_layer[:2]:
            add(client.component_id, extra.component_id, "call", "HTTP / REST", client.primary_evidence)

    return relationships


def _build_flows(
    components: list[ArchitectureComponent], relationships: list[ArchitectureRelationship]
) -> list[ArchitectureFlow]:
    by_id = {component.component_id: component for component in components}
    outgoing: dict[str, list[ArchitectureRelationship]] = {}
    for rel in relationships:
        outgoing.setdefault(rel.source, []).append(rel)

    def names(ids: list[str]) -> list[str]:
        return [by_id[cid].name for cid in ids if cid in by_id]

    clients = [component for component in components if component.kind == KIND_CLIENT]
    api = [component for component in components if component.kind in {KIND_GATEWAY, KIND_CONTROLLER}]
    api.sort(key=lambda c: c.detail.get("routes", 0), reverse=True)
    services = [component for component in components if component.kind == KIND_SERVICE]
    store_rank = {KIND_DATABASE: 0, KIND_CACHE: 1, KIND_MESSAGE_BROKER: 2}
    stores = sorted(
        [component for component in components if component.kind in store_rank],
        key=lambda c: store_rank[c.kind],
    )

    primary: list[str] = []
    if clients:
        primary.append(clients[0].component_id)
    if api:
        primary.append(api[0].component_id)
        if services:
            linked = [
                rel.target
                for rel in outgoing.get(api[0].component_id, [])
                if rel.target in {service.component_id for service in services}
            ]
            if linked:
                primary.append(linked[0])
            else:
                service_rank = {
                    service.component_id: sum(
                        1 for rel in relationships
                        if rel.source == service.component_id or rel.target == service.component_id
                    )
                    for service in services
                }
                if service_rank:
                    primary.append(max(service_rank, key=lambda cid: (service_rank[cid], by_id[cid].name)))
            if stores:
                store_links = [
                    rel.target
                    for rel in outgoing.get(primary[-1], [])
                    if rel.target in {store.component_id for store in stores}
                ]
                primary.append(store_links[0] if store_links else stores[0].component_id)

    flows: list[ArchitectureFlow] = []
    if len(primary) >= 2:
        flows.append(
            ArchitectureFlow(
                name="Primary Request Path",
                steps=names(primary),
                description=(
                    "Inbound requests enter through the API surface, are delegated to the core services, "
                    "and land in the backing data stores."
                )
                if clients
                else (
                    "Inbound requests enter through the API surface and are delegated to the core services "
                    "that own the application data."
                ),
            )
        )

    for broker in [component for component in components if component.kind == KIND_MESSAGE_BROKER]:
        references = sorted({rel.source for rel in relationships if rel.target == broker.component_id})
        if references:
            steps = [by_id[references[0]].name, broker.name]
            if len(references) > 1:
                steps.append(by_id[references[1]].name)
            flows.append(
                ArchitectureFlow(
                    name=f"Event Flow ({broker.name})",
                    steps=steps,
                    description=f"Components publish and subscribe through the {broker.name} broker.",
                )
            )
            break

    for ext in [component for component in components if component.kind == KIND_EXTERNAL]:
        callers = sorted({rel.source for rel in relationships if rel.target == ext.component_id})
        if callers:
            steps = [by_id[callers[0]].name, ext.name]
            flows.append(
                ArchitectureFlow(
                    name=f"External Integration ({ext.name})",
                    steps=steps,
                    description=f"{by_id[callers[0]].name} calls the external {ext.name} over HTTP.",
                )
            )

    api_rest = api[1:] if len(api) > 1 else []
    if api_rest and services:
        gateway = api_rest[0]
        targeted = [
            rel.target
            for rel in outgoing.get(gateway.component_id, [])
            if rel.target in {service.component_id for service in services}
        ]
        if targeted:
            chain = [gateway.name, by_id[targeted[0]].name]
            for store in stores:
                if any(
                    rel.target == store.component_id
                    for rel in outgoing.get(targeted[0], [])
                ):
                    chain.append(store.name)
                    break
            flows.append(
                ArchitectureFlow(
                    name="Secondary Request Path",
                    steps=chain,
                    description=f"{gateway.name} routes additional traffic to {by_id[targeted[0]].name}.",
                )
            )

    return flows[:4]


# ---------------------------------------------------------------------------
# Deployment and technology scans
# ---------------------------------------------------------------------------


_DEPLOYMENT_ASSET_KINDS = {
    "dockerfile", "compose", "kubernetes", "helm", "terraform", "ci",
}


def _deployment_assets(path: str, text: str) -> set[str]:
    kinds: set[str] = set()
    lower = (path or "").lower()
    base = (path or "").rsplit("/", 1)[-1].lower()
    if base.startswith("dockerfile") or base.endswith(".dockerfile"):
        kinds.add("dockerfile")
    if lower.endswith(("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")):
        kinds.add("compose")
    if lower.endswith(".tf") or "terraform" in lower:
        kinds.add("terraform")
    if lower.endswith("chart.yaml") or (lower.endswith(".yaml") and "helm" in lower):
        kinds.add("helm")
    if lower.startswith(".github/workflows/") or base in {
        ".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", ".travis.yml",
    } or lower.startswith(".circleci/"):
        kinds.add("ci")
    if lower.endswith((".yaml", ".yml")) and re.search(
        r"kind:\s*(Deployment|Service|Ingress|Pod|StatefulSet|CronJob)", text or "", re.IGNORECASE
    ):
        kinds.add("kubernetes")
    if lower.endswith((".yaml", ".yml")) and any(
        dirname in lower.split("/") for dirname in ("k8s", "kubernetes", "manifests")
    ):
        kinds.add("kubernetes")
    return kinds


def _scan_deployment(files: dict[str, str]) -> list[DeploymentComponent]:
    by_kind: dict[str, list[str]] = {kind: [] for kind in _DEPLOYMENT_ASSET_KINDS}
    cloud: list[str] = []
    for path, text in files.items():
        if _is_noise(path) or _is_sensitive(path):
            continue
        kinds = _deployment_assets(path, text)
        if not kinds:
            continue
        clean = _clean_path(path)
        for kind in kinds:
            by_kind[kind].append(clean)
        if _token_re(("aws", "azure", "gcp", "google-cloud", "cloudflare", "eks", "ecs", "aks", "gke", "kubernetes")).search(
            (text or "").lower()
        ):
            cloud.append(clean)

    deployment: list[DeploymentComponent] = []

    def add(name: str, kind: str, evidence: list[str]) -> None:
        if evidence:
            deployment.append(DeploymentComponent(name=name, kind=kind, evidence=_dedupe(evidence, limit=2)))

    add("Docker Image", "image", by_kind["dockerfile"])
    add("Service Containers (Docker Compose)", "orchestration", by_kind["compose"])
    add("Kubernetes Workloads", "platform", by_kind["kubernetes"])
    add("Helm Charts", "platform", by_kind["helm"])
    add("Infrastructure as Code (Terraform)", "iaas", by_kind["terraform"])
    add("CI / CD Pipeline", "ci", by_kind["ci"])
    if cloud:
        add("Container / Cloud Platform", "cloud", cloud)
    return deployment


def _detect_languages(files: dict[str, str]) -> list[str]:
    ext_lang = {
        ".py": "Python", ".java": "Java", ".js": "JavaScript", ".ts": "TypeScript",
        ".jsx": "React/JSX", ".tsx": "React/TSX", ".go": "Go", ".rs": "Rust",
        ".kt": "Kotlin", ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".vue": "Vue",
    }
    counts: Counter[str] = Counter()
    for path in files:
        if _is_noise(path) or _is_sensitive(path):
            continue
        lower = path.lower()
        for ext, language in ext_lang.items():
            if lower.endswith(ext):
                counts[language] += 1
                break
    manifest_lang = {
        "package.json": "JavaScript/TypeScript", "pyproject.toml": "Python",
        "requirements.txt": "Python", "pom.xml": "Java", "build.gradle": "Java",
        "go.mod": "Go", "Cargo.toml": "Rust",
    }
    for path in files:
        base = (path or "").rsplit("/", 1)[-1].lower()
        if base in manifest_lang:
            counts[manifest_lang[base]] += 2
    return [language for language, _ in counts.most_common(3)]


_TECH_IGNORED_SUBSTRINGS = (
    # "bootstrap-servers" / "bootstrap.servers" are Kafka bootstrap config keys,
    # not the Bootstrap CSS framework; blanking them removes noisy false positives.
    "bootstrap-servers",
    "bootstrap.servers",
)


def _scan_technologies(files: dict[str, str], readme: str) -> list[TechnologyEntry]:
    def normalized(text: str) -> str:
        lowered = (text or "").lower()
        for ignored in _TECH_IGNORED_SUBSTRINGS:
            lowered = lowered.replace(ignored, " ")
        return lowered

    entries: list[TechnologyEntry] = []
    languages = _detect_languages(files)
    if languages:
        entries.append(TechnologyEntry(category="Language / Runtime", name=languages[0]))
        for extra in languages[1:]:
            entries.append(TechnologyEntry(category="Language / Runtime", name=extra))
    for category, catalog in _TECH_CATALOG:
        found: dict[str, list[str]] = {}
        for label, token in catalog:
            pattern = re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE)
            evidence: list[str] = []
            for path, text in files.items():
                if _is_noise(path) or _is_sensitive(path):
                    continue
                if pattern.search(normalized(text)):
                    evidence.append(_clean_path(path))
            if not evidence and readme:
                if pattern.search(normalized(readme[:200_000])):
                    evidence = ["README"]
            if evidence:
                found[label] = _dedupe(evidence, limit=2)
        for label, evidence in sorted(found.items()):
            entries.append(TechnologyEntry(category=category, name=label, evidence=evidence))
    return entries


def _scan_protocols(files: dict[str, str]) -> list[str]:
    combined = " ".join(files.values())
    protocols = [
        label
        for label, tokens in _PROTOCOL_TOKEN_SETS
        if _token_re(tokens).search(combined.lower())
    ]
    return protocols or ["HTTP / REST"]


# ---------------------------------------------------------------------------
# Overview fields (description, style) and repository structure
# ---------------------------------------------------------------------------


def _extract_description(repo_meta: str, readme: str) -> str:
    for line in (repo_meta or "").splitlines():
        if line.lower().startswith("description:"):
            return (line.split(":", 1)[1] or "").strip()[:300]
    if readme:
        text = re.sub(r"[#>*`_~|-]", " ", readme)
        text = re.sub(r"\s+", " ", text).strip()
        plain = " ".join(
            part for part in text.split() if not re.match(r"^\[.*:(/|//|\.)", part)
        )
        return plain[:300].strip()
    return ""


def _infer_style(components: list[ArchitectureComponent]) -> str:
    """Classify the architectural style from the observed component composition.

    Returns a professional, two-part label ``"Primary style · qualifier"`` where
    the primary names a recognised architectural pattern and the qualifier adds
    the most salient supporting trait. Everything is derived from what was
    actually detected in the repository - never assumed.
    """
    kinds = [component.kind for component in components]
    kind_set = set(kinds)

    controllers = kinds.count(KIND_CONTROLLER)
    services = kinds.count(KIND_SERVICE)
    clients = kinds.count(KIND_CLIENT)
    externals = kinds.count(KIND_EXTERNAL)
    has_db = KIND_DATABASE in kind_set
    has_cache = KIND_CACHE in kind_set
    has_broker = KIND_MESSAGE_BROKER in kind_set
    has_gateway = KIND_GATEWAY in kind_set

    # Detect layer coverage (client / gateway / services / data) - a full stack
    # of distinct layers is the hallmark of a layered / n-tier design.
    layers = {component.layer for component in components}
    layered = {LAYER_GATEWAY, LAYER_SERVICES, LAYER_DATA} <= layers

    # --- Primary style ------------------------------------------------------
    if has_broker and services >= 2:
        primary = "Event-driven microservices" if services >= 4 else "Event-driven architecture"
    elif has_broker:
        primary = "Event-driven architecture"
    elif services >= 5 and controllers >= 3:
        primary = "Microservices architecture"
    elif services >= 3:
        primary = "Service-oriented architecture"
    elif clients and controllers and has_db:
        primary = "Layered n-tier web application" if layered else "Client-server web application"
    elif layered:
        primary = "Layered (n-tier) architecture"
    elif controllers and services and has_db:
        primary = "Layered backend service"
    elif controllers and services:
        primary = "REST API service"
    elif controllers:
        primary = "API-first backend"
    elif services:
        primary = "Modular service backend"
    else:
        primary = "Modular application"

    # --- Qualifier (most salient supporting trait) --------------------------
    qualifiers: list[str] = []
    if has_broker:
        qualifiers.append("asynchronous messaging")
    if has_cache and has_db:
        qualifiers.append("cached data tier")
    elif has_db:
        qualifiers.append("relational data tier")
    elif has_cache:
        qualifiers.append("in-memory store")
    if externals >= 3:
        qualifiers.append("integration-heavy")
    elif externals:
        qualifiers.append("externally integrated")
    if clients and controllers:
        qualifiers.append("full-stack")
    if not qualifiers and services:
        qualifiers.append("multi-module")

    if qualifiers:
        return f"{primary} · {qualifiers[0]}"
    return primary


_AREA_PURPOSES: tuple[tuple[set[str], str], ...] = (
    ({"frontend", "web", "ui", "client", "public"}, "User interface / client application"),
    ({"backend", "server", "api"}, "API / backend services"),
    ({"core", "domain"}, "Domain model / core business logic"),
    ({"app", "src", "application", "main"}, "Application code"),
    ({"test", "tests", "spec", "e2e", "__tests__"}, "Automated tests"),
    ({".github"}, "CI / CD workflows"),
    ({"github"}, "CI / CD workflows"),
    ({"infra", "infrastructure", "k8s", "kubernetes", "helm", "terraform", "deploy", "deployment"},
     "Deployment infrastructure"),
    ({"migrations", "db", "database", "sql", "queries"}, "Database migrations / SQL"),
    ({"config", "configuration", "conf", "settings"}, "Configuration"),
    ({"docs", "documentation"}, "Documentation"),
    ({"scripts", "tools", "tooling", "bin"}, "Tooling / automation"),
    ({"shared", "common", "lib", "libs", "packages", "utils"}, "Shared libraries / support code"),
    ({"resources", "assets", "static"}, "Non-code resources / assets"),
)


def _area_purpose(top: str) -> str:
    lowered = (top or "").lower()
    for names, purpose in _AREA_PURPOSES:
        if lowered in names:
            return purpose
    return "Application code"


def _build_structure(files: dict[str, str], tree: list[dict[str, Any]] | None) -> list[RepositoryArea]:
    prefixes: dict[str, list[str]] = {}
    all_paths = sorted(files)
    tree_paths: list[str] = []
    for entry in tree or []:
        if entry.get("type") == "file":
            tree_paths.append(str(entry.get("path") or ""))
    for path in tree_paths or all_paths:
        if _is_noise(path) or _is_sensitive(path):
            continue
        clean = _clean_path(path)
        if "/" in clean:
            prefix = clean.split("/", 1)[0]
            if prefix:
                prefixes.setdefault(prefix, []).append(clean)
        else:
            prefixes.setdefault("(root)", []).append(clean)

    areas: list[RepositoryArea] = []
    ordered = sorted(prefixes)
    for top in ordered:
        repo_files = sorted(set(prefixes[top]))
        evidence = _dedupe(repo_files, limit=2)
        if not evidence:
            continue
        if top == "(root)":
            name = "Repository root"
            purpose = "Entry points, manifests and top-level configuration"
        elif top.startswith("."):
            name = f"{top}/"
            purpose = _area_purpose(top)
        else:
            name = f"{top}/"
            purpose = _area_purpose(top)
        areas.append(RepositoryArea(name=name, purpose=purpose, evidence=evidence))
        if len(areas) >= 12:
            break
    return areas


# ---------------------------------------------------------------------------
# Security features (rule-based, evidence-backed; only present when observed)
# ---------------------------------------------------------------------------

_SECURITY_RULES: tuple[
    tuple[str, str, str, tuple[str, ...]], ...
] = (
    # -- Authentication -------------------------------------------------------
    (
        "Authentication",
        "JWT tokens",
        "Token-based authentication using signed JWTs.",
        ("jwt", "jsonwebtoken", "jjwt", "passport-jwt"),
    ),
    (
        "Authentication",
        "OAuth2 / OpenID Connect",
        "Delegated identity via an OAuth/OIDC flow or provider SDK.",
        ("oauth", "oidc", "openid", "oauth2", "keycloak", "auth0", "cognito", "okta"),
    ),
    (
        "Authentication",
        "Session-based auth",
        "Server-side session or cookie-based login.",
        ("session", "flask-login", "express-session", "passport", "devise", "django.contrib.auth"),
    ),
    (
        "Authentication",
        "API key auth",
        "Clients authenticate with a shared API key supplied on requests.",
        ("api-key", "x-api-key", "apikey", "api key"),
    ),
    (
        "Authentication",
        "Mutual TLS (mTLS)",
        "Client certificates / mutual TLS used at the transport layer.",
        ("mtls", "mutual tls", "clientcertificate", "client certificate", "ssl_context"),
    ),
    (
        "Authentication",
        "SAML",
        "SAML-based federation for enterprise identity.",
        ("saml", "spring-security-saml"),
    ),
    # -- Password / secret handling -------------------------------------------
    (
        "Password & Secrets",
        "Hashed passwords (bcrypt / argon2 / scrypt)",
        "Stored passwords are never plain text; a key-derivation hash is used.",
        ("bcrypt", "argon2", "scrypt", "pbkdf2", "passlib", "werkzeug.security"),
    ),
    (
        "Password & Secrets",
        "Environment-based secrets",
        "Secrets read from environment variables / config, not committed.",
        ("os.getenv", "os.environ", "getenv(", "process.env", "dotenv", "settings.py"),
    ),
    (
        "Password & Secrets",
        "External secrets manager",
        "Secrets sourced from a dedicated vault / secrets store.",
        ("vault", "secretsmanager", "secret manager", "keyvault", "parameter store"),
    ),
    # -- Authorization --------------------------------------------------------
    (
        "Authorization",
        "Role-based access control (RBAC)",
        "Access is gated by roles/permissions on operations.",
        ("rbac", "rolesallowed", "preauthorize", "hasrole", "hasrole(", "role=", "required_permission"),
    ),
    (
        "Authorization",
        "Route / method guards",
        "Guards or middlewares protect routes and handlers.",
        ("@useguards", "requires_role", "requires_auth", "is_authenticated", "login_required", "authorize("),
    ),
    # -- Input validation -----------------------------------------------------
    (
        "Input Validation",
        "Schema validation",
        "Inbound payloads are validated against a schema before use.",
        ("pydantic", "marshmallow", "jsonschema", "cerberus", "voluptuous", "zod", "yup", "class-validator", "@valid"),
    ),
    # -- Transport / browser security -----------------------------------------
    (
        "Transport & Browser",
        "HTTPS / TLS enforcement",
        "Transport-layer encryption or TLS context is configured.",
        ("ssl", "tls", "https", "secure socket", "ssl_context"),
    ),
    (
        "Transport & Browser",
        "HTTP security headers",
        "Helmet (Node) or equivalent sets CSP / framing / MIME-sniffing headers.",
        ("helmet", "content-security-policy", "x-frame-options", "x-content-type-options", "strict-transport-security"),
    ),
    (
        "Transport & Browser",
        "Secure cookie flags",
        "Cookies marked HttpOnly / SameSite / Secure.",
        ("httponly", "samesite", "secure="),
    ),
    (
        "Transport & Browser",
        "CORS configuration",
        "Cross-origin access is explicitly configured, not left wide open.",
        ("cors(", "allow_origins", "cross_origin", "corsorigins", "allow_methods"),
    ),
    # -- Threat protection ----------------------------------------------------
    (
        "Threat Protection",
        "CSRF protection",
        "Cross-site request forgery tokens / verification are in place.",
        ("csrf", "xsrf"),
    ),
    (
        "Threat Protection",
        "Rate limiting",
        "Request throttling limits abuse and brute force.",
        ("rate limit", "ratelimit", "throttle", "limiter", "too many requests"),
    ),
    (
        "Threat Protection",
        "Parameterized queries",
        "Data access uses parameter binding / prepared statements (SQLi resistance).",
        ("preparedstatement", "prepareStatement", "parameterized", "sqlalchemy.text", "cursor.execute", "binds="),
    ),
    (
        "Threat Protection",
        "Output sanitization",
        "User-controlled output is escaped / sanitized to resist XSS.",
        ("bleach", "sanitize(", "markupsafe", "strip_tags", "escape("),
    ),
    # -- Audit & scanning tooling ---------------------------------------------
    (
        "Audit & Tooling",
        "Dependency vulnerability scanning",
        "Tooling watches dependencies for known vulnerabilities.",
        ("dependabot", "snyk", "safety", "trivy", "renovate", "owasp dependency"),
    ),
    (
        "Audit & Tooling",
        "Static security scanning",
        "Static analysis / secret scanning is wired into the pipeline.",
        ("bandit", "gitleaks", "semgrep", "codeql", "zaproxy", "zap"),
    ),
)


def _scan_security(files: dict[str, str]) -> list[SecurityFeature]:
    found: dict[tuple[str, str, str], list[str]] = {}
    for path, text in files.items():
        if _is_noise(path) or _is_sensitive(path):
            continue
        clean = _clean_path(path)
        lowered = (text or "").lower()
        lower_path = (path or "").lower()
        for category, name, description, tokens in _SECURITY_RULES:
            if _token_re(tokens).search(lowered):
                found.setdefault((category, name, description), []).append(clean)
        if "dependabot" in lower_path:
            found.setdefault(
                ("Audit & Tooling", "Dependency vulnerability scanning",
                 "Tooling watches dependencies for known vulnerabilities."),
                [],
            ).append(clean)
        if "codeql" in lower_path:
            found.setdefault(
                ("Audit & Tooling", "Static security scanning",
                 "Static analysis / secret scanning is wired into the pipeline."),
                [],
            ).append(clean)

    features: list[SecurityFeature] = []
    for (category, name, description), paths in found.items():
        features.append(
            SecurityFeature(
                category=category,
                name=name,
                description=description,
                evidence=_dedupe(paths, limit=2),
            )
        )
    return features[:14]


# ---------------------------------------------------------------------------
# Repository tree / code-flow map
# ---------------------------------------------------------------------------

_DIR_ROLES = {
    "frontend": "UI", "web": "UI", "ui": "UI", "client": "UI", "public": "static",
    "backend": "backend", "server": "backend", "api": "API", "app": "app", "src": "app",
    "core": "domain", "domain": "domain", "models": "domain",
    "tests": "tests", "test": "tests", "spec": "tests",
    ".github": "CI", "workflows": "CI",
    "infra": "infra", "infrastructure": "infra", "k8s": "infra",
    "helm": "infra", "terraform": "infra", "deploy": "infra", "deployment": "infra",
    "migrations": "db", "db": "db", "database": "db", "sql": "db",
    "config": "config", "configuration": "config", "conf": "config", "settings": "config",
    "docs": "docs", "documentation": "docs",
    "shared": "shared", "common": "shared", "lib": "shared", "libs": "shared",
    "packages": "shared", "utils": "shared", "scripts": "tooling", "tools": "tooling",
    "routes": "routes", "controllers": "routes", "handlers": "routes",
    "services": "services", "service": "services",
    "repositories": "data", "repository": "data", "dao": "data", "persistence": "data",
    "middleware": "middleware",
}


def _dir_role(parts: list[str]) -> str:
    current = (parts[-1] or "").lower() if parts else ""
    role = _DIR_ROLES.get(current, "")
    if not role and parts:
        role = _DIR_ROLES.get((parts[0] or "").lower(), "")
    return role


_MAX_TREE_DEPTH = 4
_MAX_TREE_NODES = 48


def _kind_label(kind: str) -> str:
    return {
        KIND_CLIENT: "client",
        KIND_GATEWAY: "entry",
        KIND_CONTROLLER: "routes",
        KIND_SERVICE: "service",
        KIND_DATABASE: "data",
        KIND_CACHE: "cache",
        KIND_MESSAGE_BROKER: "broker",
        KIND_EXTERNAL: "external",
        KIND_CROSS_CUTTING: "cross",
    }.get(kind, "")


def _build_tree(
    files: dict[str, str],
    tree: list[dict[str, Any]] | None,
    components: list[ArchitectureComponent],
) -> list[RepositoryNode]:
    role_by_path: dict[str, str] = {}
    note_by_path: dict[str, str] = {}
    for component in components:
        for path in component.evidence:
            role_by_path.setdefault(path, _kind_label(component.kind))
            if component.kind in (KIND_DATABASE, KIND_CACHE, KIND_MESSAGE_BROKER, KIND_EXTERNAL):
                note_by_path.setdefault(path, component.name)

    file_paths: list[str] = []
    if tree:
        file_paths = [
            str(entry.get("path") or "")
            for entry in tree
            if entry.get("type") == "file"
        ]
    if not file_paths:
        file_paths = sorted(files)
    file_paths = [
        _clean_path(path)
        for path in file_paths
        if path and not _is_noise(path) and not _is_sensitive(path)
    ]

    emitted_dirs: set[str] = set()
    nodes: list[RepositoryNode] = []
    for path in sorted(file_paths):
        parts = path.split("/")
        for index in range(1, len(parts)):
            prefix = "/".join(parts[:index])
            if prefix in emitted_dirs:
                continue
            emitted_dirs.add(prefix)
            nodes.append(
                RepositoryNode(
                    name=f"{parts[index - 1]}/",
                    depth=index,
                    is_dir=True,
                    role=_dir_role(parts[:index]),
                )
            )
        role = role_by_path.get(path, "")
        note = note_by_path.get(path, "")
        nodes.append(
            RepositoryNode(
                name=parts[-1],
                depth=len(parts),
                is_dir=False,
                role=role,
                note=note,
            )
        )

    # Keep the map usefully small: directories up to depth 3 and files up to
    # depth 4 always, plus role-bearing files (entry/routes/service/data/external)
    # at any depth. Deeper files without a role are omitted.
    kept: list[RepositoryNode] = []
    for node in nodes:
        if node.is_dir:
            if node.depth <= 3 and node.depth < _MAX_TREE_DEPTH:
                kept.append(node)
        elif node.depth <= 4 or node.role or node.note:
            kept.append(node)
    kept.sort(key=lambda node: (node.depth, (0 if node.is_dir else 1), node.name.lower()))
    return kept[:_MAX_TREE_NODES]


# ---------------------------------------------------------------------------
# Build entry point
# ---------------------------------------------------------------------------


def build_architecture_model(
    repo: str,
    *,
    files: dict[str, str],
    readme: str = "",
    repo_meta: str = "",
    tree: list[dict[str, Any]] | None = None,
) -> ArchitectureModel:
    """Construct the evidence-driven architecture model for a repository.

    Only the sampled file contents are used. Components, relationships, flows,
    technologies and deployment elements are derived from real identifiers,
    tokens and paths; anything not observed is simply absent from the model.
    """
    files = {
        _clean_path(path): (text or "")
        for path, text in files.items()
        if path and not _is_noise(path) and not _is_sensitive(path)
    }

    entry_components = _entry_components(files)
    controller_components = _controller_components(files)
    service_components = _service_components(files, controller_components)
    datastores = _datastore_components(files)
    caches = _cache_components(files)
    brokers = _broker_components(files)
    externals = _external_components(files)
    cross_cutting = _cross_cutting_components(files)
    clients = _client_components(files, tree)

    components = _sort_components(
        [
            *clients,
            *entry_components,
            *controller_components,
            *service_components,
            *datastores,
            *caches,
            *brokers,
            *externals,
            *cross_cutting,
        ]
    )
    components = _unique_ids(components)

    relationships = _build_relationships(files, components)
    flows = _build_flows(components, relationships)
    technologies = _scan_technologies(files, readme)
    deployment = _scan_deployment(files)
    protocols = _scan_protocols(files)
    notes = _build_notes(files, readme, clients, datastores, deployment, externals)

    return ArchitectureModel(
        repo=_clean_path(repo),
        components=components,
        relationships=relationships,
        flows=flows,
        technologies=technologies,
        deployment=deployment,
        protocols=protocols,
        notes=notes,
        description=_extract_description(repo_meta, readme),
        style=_infer_style(components),
        structure=_build_structure(files, tree),
        security=_scan_security(files),
        tree=_build_tree(files, tree, components),
    )


def _sort_components(components: list[ArchitectureComponent]) -> list[ArchitectureComponent]:
    layer_order = {
        LAYER_CLIENT: 0, LAYER_GATEWAY: 1, LAYER_SERVICES: 2, LAYER_DATA: 3, LAYER_EXTERNAL: 4,
    }

    def rank(component: ArchitectureComponent) -> tuple[int, int, str]:
        base = layer_order.get(component.layer, 9)
        if component.kind == KIND_CROSS_CUTTING:
            base = 5
        if base == 1 and component.detail.get("routes", 0) > 0:
            primary = 0
        elif component.kind == KIND_SERVICE:
            primary = 1
        else:
            primary = 2
        return (base, primary, component.name.lower())

    return sorted(components, key=rank)


def _build_notes(
    files: dict[str, str],
    readme: str,
    clients: list[ArchitectureComponent],
    datastores: list[ArchitectureComponent],
    deployment: list[DeploymentComponent],
    externals: list[ArchitectureComponent],
) -> list[str]:
    notes: list[str] = []
    if not clients:
        notes.append("No client-side code was observed; the UI layer is not shown.")
    if not datastores:
        notes.append("No database/storage technology was observed in the sampled files.")
    if not deployment:
        notes.append("No container, orchestration or CI/CD assets were observed; deployment is not mapped.")
    if not externals:
        notes.append(
            "No runtime-external systems were observed; package registries and documentation "
            "URLs are not modelled as runtime dependencies."
        )
    if not readme:
        notes.append("No README was retrieved; descriptions were inferred from sampled source only.")
    if not notes:
        notes.append("Architecture derived strictly from the sampled repository files.")
    return notes
