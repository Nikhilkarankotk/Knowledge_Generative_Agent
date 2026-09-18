"""Application configuration.

Mirrors the Java ``application.yml`` and environment variables (e.g. ``MISTRAL_API_KEY``)
so that the Python implementation can be dropped in as a replacement without renaming
configuration that already exists in the deployment environment.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---
    app_name: str = "Users"
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # --- Database (matches src/main/resources/application.yml) ---
    db_host: str = "localhost"
    db_port: int = 5433
    db_name: str = "Knowledge_Gen_Agent"
    db_user: str = "postgres"
    db_password: str = "postgres"
    db_driver: str = "postgresql"
    # Optional full override, e.g. postgresql://postgres:postgres@localhost:5433/Knowledge_Gen_Agent
    database_url: str | None = None
    # Set to true to echo SQL (mirrors spring.jpa.show-sql: true)
    database_echo_sql: bool = False

    # --- Mistral AI (matches `mistral.api.*` in application.yml) ---
    mistral_api_key: str = ""
    mistral_base_url: str = "https://api.mistral.ai/v1"
    mistral_chat_model: str = "open-mistral-nemo"
    mistral_embedding_model: str = "mistral-embed"
    mistral_ocr_model: str = "mistral-ocr-latest"
    mistral_timeout_seconds: float = 30.0
    mistral_retries: int = 3

    # --- RAG ---
    rag_chunk_size: int = 500
    rag_top_k: int = 5

    # --- RAG answer-generation LLM (Azure OpenAI, "luna" deployment) ---
    # When RAG_LLM_PROVIDER=azure_openai (with a key, endpoint and deployment set),
    # the final RAG answer generation is routed to an Azure OpenAI chat deployment
    # (e.g. the "luna" ChatGPT deployment) instead of Mistral. Embeddings and OCR
    # always keep using the Mistral client above; only the RAG answer LLM changes.
    # Leave RAG_LLM_PROVIDER empty (or "mistral") to keep using Mistral for answers.
    rag_llm_provider: str = ""
    azure_openai_api_key: str = ""
    # Azure AI Foundry OpenAI-compatible endpoint. For the Foundry v1 surface use
    # the ".../openai/v1" base URL, e.g.
    #   https://<resource>.services.ai.azure.com/openai/v1
    # (the model/deployment name is sent in the request body, not the URL path).
    azure_openai_endpoint: str = ""
    # The deployment / model name sent in the body, e.g. "gpt-5.6-luna".
    azure_openai_deployment: str = "gpt-5.6-luna"
    # Leave empty for the Foundry "/openai/v1" surface. Set an api-version only for
    # classic "*.openai.azure.com/openai/deployments/..." endpoints.
    azure_openai_api_version: str = ""
    azure_openai_timeout_seconds: float = 60.0
    azure_openai_retries: int = 2
    azure_openai_max_tokens: int = 1024
    azure_openai_temperature: float = 0.2
    # Some models (e.g. the gpt-5.* "luna" family) only accept the default
    # temperature; keep this False so no temperature is sent. Set True for models
    # that accept a custom temperature.
    azure_openai_supports_temperature: bool = False

    # --- Export (source-aware, context-grounded export service) ---
    # Depot limits for export output. The ExportService validates the proposed
    # export (native files, generated documents, archives) against these.
    export_max_files: int = 100
    export_max_total_size_mb: int = 100
    export_max_single_file_size_mb: int = 25
    export_max_generated_report_size_mb: int = 10
    export_max_github_source_size_mb: int = 25
    # Contexts older than this (in days) can no longer be exported.
    export_context_ttl_days: int = 30
    # When true, ZIP/manifest exports also include a copy of the assistant's
    # answer (export-summary.md). Optional per the export specification.
    export_include_summary: bool = True
    # When true, the ExportService may ask the LLM for a recommended export
    # intent (format/type) that is then validated against the retrieved sources
    # and the available exporters before it is executed. A validated
    # deterministic proposal is always used as the fallback.
    export_intent_recommendation_enabled: bool = True
    export_intent_recommendation_timeout_seconds: float = 15.0
    # When true, generated exports (GitHub reports and Confluence/SharePoint/upload
    # documents) include an LLM-written narrative (functionality/architecture/tech
    # stack for repositories; an elaborated overview for documents) that is grounded
    # in the retrieved evidence. A deterministic evidence section always remains.
    export_report_synthesis_enabled: bool = True

    # --- Conversation memory ---
    conversation_max_history: int = 10

    # --- Knowledge Generative Agent (Semantic Kernel, Phase 1) ---
    # When enabled, ChatService routes user messages through the Semantic Kernel
    # KnowledgeGenerativeAgent (KnowledgePlugin -> RAG + ConfluencePlugin -> Confluence).
    sk_agent_enabled: bool = True
    # Max parallel auto-invocation rounds Semantic Kernel may perform per turn.
    sk_max_auto_invoke_attempts: int = 10
    # Per-turn timeout for the agent (includes tool calls + final completion).
    # Generous by default: with many allowlisted GitHub repositories the agent may
    # need several auto-invocation rounds, each an LLM round-trip.
    sk_agent_timeout_seconds: float = 120.0

    # --- Confluence (ConfluencePlugin, Phase 1) ---
    # Disabled by default: plugins return "not configured" markers when off.
    confluence_enabled: bool = False
    confluence_base_url: str = ""
    confluence_api_token: str = ""
    confluence_username: str = ""
    confluence_limit: int = 5
    confluence_timeout_seconds: float = 15.0
    confluence_page_char_limit: int = 15000
    # Whether Confluence searches also include drafts. Off by default: drafts are
    # usually not the authoritative source an agent should answer from.
    confluence_include_drafts: bool = False

    # --- GitHub (GitHubPlugin, Phase 2) ---
    # Disabled by default: plugins return "not configured" markers when off. The
    # integration is read-only; use a personal access token with read scopes only.
    github_enabled: bool = False
    github_token: str = ""
    github_base_url: str = "https://api.github.com"
    github_limit: int = 5
    github_timeout_seconds: float = 15.0
    github_repo_char_limit: int = 10000
    # Comma-separated allowlist of repositories (owner/repo) the agent may read.
    # Everything outside this list is rejected before any API call and global
    # repository/code search is disabled. Leave empty to disable GitHub repository
    # access entirely (the agent answers "No GitHub repositories are configured").
    github_allowed_repositories: str = ""

    # --- GitHub report analysis LLM (OpenRouter / Anthropic Foundry, Phase 2) ---
    # Optional *separate* LLM provider used to write the GitHub repository analysis
    # narrative (ReportSynthesizer). Leave GITHUB_LLM_PROVIDER empty or set it to
    # "mistral" to keep using the Mistral synthesis client; set it to:
    #   * "openrouter"         -> OpenRouter (OpenAI-compatible chat completions)
    #   * "anthropic_foundry"  -> Claude on Azure AI Foundry (e.g. claude-opus-4-8)
    # This only affects the generated report narrative - chat, RAG and embeddings
    # always keep using the Mistral client above.
    github_llm_provider: str = ""
    github_llm_model: str = "openrouter/free"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_timeout_seconds: float = 60.0
    openrouter_retries: int = 2

    # --- Anthropic on Azure AI Foundry (Claude Opus report synthesis) ---
    # Used when GITHUB_LLM_PROVIDER=anthropic_foundry. The Foundry Anthropic surface
    # is ".../anthropic" (the client appends /v1/messages) and authenticates with the
    # x-api-key header. The model name (e.g. "claude-opus-4-8") is sent in the body.
    anthropic_foundry_api_key: str = ""
    anthropic_foundry_endpoint: str = ""
    anthropic_foundry_model: str = "claude-opus-4-8"
    anthropic_foundry_version: str = "2023-06-01"
    anthropic_foundry_timeout_seconds: float = 90.0
    anthropic_foundry_retries: int = 2
    # Optional sampling temperature; leave None to use the model default.
    anthropic_foundry_temperature: float | None = None
    # Bounds for the repository analysis that feeds the report narrative.
    # Analysis is a deep, asynchronous operation: breadth is preferred over
    # latency. This is a high safety ceiling; the byte budget governs how much
    # is actually read.
    github_analysis_max_files: int = 300
    github_analysis_max_context_chars: int = 60000
    github_analysis_max_tokens: int = 3000
    github_analysis_temperature: float = 0.1

    # --- SharePoint Online (SharePointPlugin, Phase 3) ---
    # Disabled by default: plugins return "not configured" markers when off. The
    # integration is read-only and app-only (Microsoft Graph client credentials
    # flow). When SHAREPOINT_TENANT_WIDE=false (default) the scope is the single
    # configured site + knowledge-base folder allowlists (Sites.Selected only) and
    # no tenant-wide site discovery/search is used. When SHAREPOINT_TENANT_WIDE=true
    # the site/folder allowlists are not enforced and the agent may search and read
    # every SharePoint site and document the application's Microsoft Graph
    # permission can access (requires the Entra app to hold the broader read
    # permission, e.g. Sites.Read.All / Files.Read.All for the search JSON API).
    sharepoint_enabled: bool = False
    sharepoint_tenant_wide: bool = False
    # Azure region of the Microsoft 365 tenant, required by the Microsoft Graph
    # Search API when using application permissions (POST /search/query answers
    # HTTP 400 "Region is required when request with application permission"
    # without it). Discover it from the error message / tenant home region, e.g.
    # "IND". Only used in tenant-wide mode.
    sharepoint_search_region: str = ""
    sharepoint_tenant_id: str = ""
    sharepoint_client_id: str = ""
    sharepoint_client_secret: str = ""
    sharepoint_graph_base_url: str = "https://graph.microsoft.com/v1.0"
    # The SharePoint site to target is resolved at runtime with the documented
    # Graph form: GET /sites/{hostname}:/{relative-path} (never hard-coded in
    # business logic). E.g. hostname "knowledgegenagent.sharepoint.com" and
    # relative path "/sites/KnowledgeGenAgent".
    sharepoint_site_hostname: str = ""
    sharepoint_site_relative_path: str = ""
    # Semicolon-separated allowlist of SharePoint site IDs (Graph site-id form
    # "host.sharepoint.com,<siteId>,<webId>", as resolved through Microsoft Graph)
    # the agent may access. Entries are separated with ";" because each site id
    # itself contains commas. Everything outside this list is rejected before any
    # further Graph request. Leave empty to disable SharePoint access entirely (the
    # agent answers "No SharePoint sites are configured").
    sharepoint_allowed_sites: str = ""
    # Semicolon-separated allowlist of knowledge-base folders (relative to the
    # site's primary document library root) the agent may list/search/read. This is
    # an APPLICATION-LEVEL restriction on top of the Sites.Selected permission:
    # retrieval never leaves the configured folder(s). E.g.
    # "sharepoint-rag-knowledge-base". Leave empty (and SHAREPOINT_ENABLED=false)
    # to disable SharePoint access entirely.
    sharepoint_allowed_folders: str = ""
    sharepoint_limit: int = 10
    sharepoint_timeout_seconds: float = 30.0
    sharepoint_content_char_limit: int = 20000

    @property
    def sharepoint_allowed_sites_list(self) -> list[str]:
        """Parsed, normalized site allowlist from ``SHAREPOINT_ALLOWED_SITES``.

        Entries are separated by ``;`` so a single entry may contain commas (the
        Graph site-id form is ``host,siteId,webId``). Trims whitespace and
        surrounding slashes, ignores empty entries and de-duplicates
        case-insensitively without collapsing punctuation.
        """
        result: list[str] = []
        seen: set[str] = set()
        for entry in self.sharepoint_allowed_sites.split(";"):
            site = _normalize_site_id(entry)
            if not site:
                continue
            if site.lower() in seen:
                continue
            seen.add(site.lower())
            result.append(site)
        return result

    @property
    def sharepoint_allowed_folders_list(self) -> list[str]:
        """Parsed, normalized folder allowlist from ``SHAREPOINT_ALLOWED_FOLDERS``.

        Folders are drive-relative paths (e.g. ``sharepoint-rag-knowledge-base``)
        separated by ``;``. The special entry ``.`` names the root of the
        Documents library (the "Shared Documents" view). Unsafe entries (absolute
        paths, traversal sequences or Graph resource tokens) are dropped so the
        configured list can only ever name plain relative folder paths.
        """
        result: list[str] = []
        seen: set[str] = set()
        for entry in self.sharepoint_allowed_folders.split(";"):
            folder = _normalize_folder_id(entry)
            if not folder:
                continue
            if folder.lower() in seen:
                continue
            seen.add(folder.lower())
            result.append(folder)
        return result

    @property
    def github_allowed_repository_list(self) -> list[str]:
        """Parsed, normalized allowlist from ``GITHUB_ALLOWED_REPOSITORIES``.

        Trims whitespace and surrounding slashes, ignores empty entries and
        de-duplicates case-insensitively. Full URLs and trailing ``.git`` suffixes
        are normalized so ``https://github.com/owner/repo.git`` and
        ``owner/repo`` compare equal.
        """
        result: list[str] = []
        seen: set[str] = set()
        for entry in self.github_allowed_repositories.split(","):
            repo = _normalize_repository_id(entry)
            if not repo:
                continue
            if repo.lower() in seen:
                continue
            seen.add(repo.lower())
            result.append(repo)
        return result

    # --- CORS (matches CorsConfig.java) ---
    cors_origins: list[str] = ["http://localhost:4200"]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def sqlalchemy_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        if self.db_driver == "postgresql":
            return (
                f"postgresql://{self.db_user}:{self.db_password}"
                f"@{self.db_host}:{self.db_port}/{self.db_name}"
            )
        return f"{self.db_driver}://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def is_postgres(self) -> bool:
        return self.sqlalchemy_database_url.startswith(("postgresql", "postgres"))

    @model_validator(mode="after")
    def _validate_sharepoint_config(self) -> Settings:
        """Fail fast on an enabled but incomplete SharePoint configuration.

        When ``SHAREPOINT_ENABLED`` is true every required SharePoint setting must
        be present. In scoped mode (default) that is the full set: tenant, client,
        secret, site hostname/relative path, the site allowlist and the folder
        allowlist. In tenant-wide mode (``SHAREPOINT_TENANT_WIDE=true``) only the
        tenant, client, secret and search region are required because the
        site/folder allowlists are not enforced. A disabled SharePoint integration
        never blocks startup.
        """
        if not self.sharepoint_enabled:
            return self
        missing: list[str] = []
        if not self.sharepoint_tenant_id.strip():
            missing.append("SHAREPOINT_TENANT_ID")
        if not self.sharepoint_client_id.strip():
            missing.append("SHAREPOINT_CLIENT_ID")
        if not self.sharepoint_client_secret.strip():
            missing.append("SHAREPOINT_CLIENT_SECRET")
        if self.sharepoint_tenant_wide:
            if not self.sharepoint_search_region.strip():
                missing.append("SHAREPOINT_SEARCH_REGION")
            if missing:
                raise ValueError(
                    "Invalid SharePoint configuration: "
                    "SHAREPOINT_ENABLED=true with SHAREPOINT_TENANT_WIDE=true but "
                    "the following required settings are missing or empty: "
                    + ", ".join(missing)
                )
            return self
        if not self.sharepoint_site_hostname.strip():
            missing.append("SHAREPOINT_SITE_HOSTNAME")
        if not self.sharepoint_site_relative_path.strip():
            missing.append("SHAREPOINT_SITE_RELATIVE_PATH")
        if not self.sharepoint_allowed_sites_list:
            missing.append("SHAREPOINT_ALLOWED_SITES")
        if not self.sharepoint_allowed_folders_list:
            missing.append("SHAREPOINT_ALLOWED_FOLDERS")
        if missing:
            raise ValueError(
                "Invalid SharePoint configuration: SHAREPOINT_ENABLED=true but the "
                "following required settings are missing or empty: "
                + ", ".join(missing)
            )
        return self


def _normalize_repository_id(value: str) -> str:
    """Normalize a repository identifier to ``owner/name`` form.

    Accepts ``owner/name``, full URLs (``https://github.com/owner/name``) and
    trailing ``.git``; strips whitespace and surrounding slashes. Returns ``""``
    for empty or unusable inputs.
    """
    repo = (value or "").strip().strip("/")
    lower = repo.lower()
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "www.github.com/",
        "github.com/",
    ):
        if lower.startswith(prefix):
            repo = repo[len(prefix):]
            break
    if repo.endswith(".git"):
        repo = repo[: -len(".git")]
    return repo.strip()


def _normalize_site_id(value: str) -> str:
    """Normalize a SharePoint site identifier (``host,siteId,webId`` form).

    Trims whitespace and surrounding slashes and returns ``""`` for empty or
    unusable inputs. The three-segment Graph site-id form used by the
    sites/{site-id} endpoints is preserved as-is (punctuation is significant).
    """
    return (value or "").strip().strip("/")


def _normalize_folder_id(value: str) -> str:
    """Normalize a SharePoint folder allowlist entry.

    Returns a plain drive-relative folder path (e.g. ``sharepoint-rag-knowledge-base``)
    with whitespace and surrounding slashes trimmed, or ``""`` for empty or unsafe
    inputs. Absolute paths, ``..`` traversal sequences, backslashes and Graph
    resource tokens are rejected so the allowlist can only name plain relative paths.
    """
    folder = (value or "").strip()
    if folder.startswith(("/", "\\")) or "\\" in folder:
        return ""
    folder = folder.strip("/")
    # "." (or "root") names the root of the Documents library itself, i.e. the
    # "Shared Documents" view, for libraries whose files are not in a subfolder.
    if folder in {".", "root", "ROOT", "Shared Documents", "shared documents"}:
        return "."
    lower = folder.lower()
    if not folder:
        return ""
    if ".." in folder.split("/"):
        return ""
    if ":" in folder or "@" in folder:
        return ""
    if lower.startswith(("http://", "https://", "sites/")):
        return ""
    return folder


@lru_cache
def get_settings() -> Settings:
    return Settings()
